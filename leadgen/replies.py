"""Automatic replies, from day one (operator decision 2026-10-22; supersedes the
studio's template-only reply rule for this product).

tick polls every mailbox over IMAP, matches replies to our sends by
In-Reply-To/References (falling back to the sender address), classifies with
code rules first (bounce, auto-reply/OOO, opt-out) and then a Haiku classifier,
and acts per class. The AI answers every reply in a thread up to the campaign's
cap (default 4); the cap, not the first reply, is what hands a thread to Marcus.
Every AI reply is threaded from the same mailbox and BCCs Marcus.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr

from .campaigns import campaign_link
from .compliance import ComplianceError
from .llm import STR, STRS, BudgetStop, LLMError, obj
from .sender import add_business_days, send_reply, today_et
from .store import msgid_key, utcnow
from .writer import BANNED, LINK_TOKEN

log = logging.getLogger(__name__)

OPT_OUT = re.compile(r"\b(remove me|remove|unsubscribe|stop emailing|opt[\s-]?out|take me off|"
                     r"don'?t (?:contact|email)|do not (?:contact|email)|no more emails)\b", re.I)
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery", re.I)
FAILED_RCPT = re.compile(r"(?:Final-Recipient|Original-Recipient):\s*rfc822;\s*<?([^\s>]+)", re.I)
OOO = re.compile(r"\b(out of (?:the )?office|on vacation|away from (?:the )?office|auto(?:matic)?[- ]?reply|"
                 r"i am currently away|i'm currently away|limited access to email|will return on|"
                 r"back in the office)\b", re.I)
CLASSIFY_SCHEMA = obj({"class": {"type": "string", "enum": ["interested", "question", "not_now", "referral",
                                                              "pricing", "legal", "negative", "unclear"]},
                       "confidence": {"type": "number"}, "referral_email": STR, "summary": STR})
REPLY_SCHEMA = obj({"body": STR, "answerable": {"type": "boolean"}})
REPLY_CRITIC_SCHEMA = obj({"pass": {"type": "boolean"}, "issues": STRS})
CLASSES = ("interested", "question", "not_now", "referral", "pricing", "legal", "negative", "unclear")
AUTO_REPLY = {"interested", "question", "not_now", "referral"}
POSITIVE = {"interested", "question", "referral"}
HUMAN_Q = re.compile(r"\b(are you (?:a )?(?:real|human|person|bot|robot|ai)|is this (?:a )?(?:bot|ai|automated|real person)|"
                     r"am i talking to (?:a )?(?:bot|ai|person|human))\b", re.I)
HOW_GOT = re.compile(r"\bhow (?:did|do) you (?:get|find|have) (?:my|our) (?:email|address|info)\b", re.I)
DATE_PROMISE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|"
                          r"next week|this week|\d{1,2}(?::\d{2})?\s?(?:am|pm)|i(?:'ll| will) (?:call|send|have|get))\b", re.I)


# ------------------------------------------------------------------ parsing
def _hdr(msg, name: str) -> str:
    return str(make_header(decode_header(msg.get(name, "") or "")))


def body_text(msg) -> str:
    parts = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("text/plain", "message/delivery-status") or ctype.startswith("text/"):
            payload = part.get_payload(decode=True)
            if payload:
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                if ctype == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                parts.append(text)
            elif ctype == "message/delivery-status":
                parts.append(str(part))
    return "\n".join(parts)


def fresh_text(body: str) -> str:
    """Only the reply itself counts, not the quoted original (which contains 'unsubscribe')."""
    return re.split(r"\n\s*(?:On .{0,200}wrote:|>|-----Original Message-----|From: .+\n)", body, maxsplit=1)[0].strip()


def rule_class(msg) -> tuple[str | None, str | None]:
    """Code rules first. Returns (kind, address): bounce | auto | optout | None."""
    sender = parseaddr(_hdr(msg, "From"))[1].lower()
    subject = _hdr(msg, "Subject")
    body = body_text(msg)
    ctype = msg.get_content_type()
    if BOUNCE_FROM.search(sender) or "report-type=delivery-status" in str(msg.get("Content-Type", "")).lower() \
            or re.search(r"undeliverable|delivery status notification|returned mail|failure notice", subject, re.I):
        m = FAILED_RCPT.search(body)
        return "bounce", (m.group(1).lower() if m else None)
    auto_sub = (msg.get("Auto-Submitted") or "").lower()
    if (auto_sub and auto_sub != "no") or msg.get("X-Autoreply") or msg.get("X-Autorespond") \
            or (msg.get("Precedence") or "").lower() in ("auto_reply", "bulk", "junk") \
            or OOO.search(subject) or OOO.search(fresh_text(body)[:600]):
        return "auto", sender
    if OPT_OUT.search(subject) or OPT_OUT.search(fresh_text(body)):
        return "optout", sender
    del ctype
    return None, sender


CLASSIFY_SYSTEM = """Classify a reply to a cold email from a small marketing studio. Return JSON only:
{"class": "interested|question|not_now|referral|pricing|legal|negative|unclear",
 "confidence": 0.0-1.0, "referral_email": "address if they point to a colleague, else empty",
 "summary": "one line"}
interested = wants to talk / try it / asks how it works with intent; question = asks something about the offer;
not_now = polite no for now; referral = points to someone else; pricing = asks about price or cost;
legal = threats, compliance, "remove my data", lawyers, spam complaints; negative = annoyed or hostile;
unclear = none of these fit."""

REPLY_SYSTEM = """You are an assistant that answers replies to {sender}'s emails at {company}. Write the reply body only.
Reply class: {klass}. Goal: {goal}
Rules you must follow exactly:
- Use ONLY the offer facts and FAQ given. No other claims, numbers, prices, dates, times or commitments.
- At most {max_words} words. Plain, warm, brief. No exclamation marks, no hype, no pressure, no em or en dashes.
- {link_rule}
- Never promise a call, a time, a date, a document, or anything beyond the link.
- If they ask whether a person is writing, include this sentence exactly: "{disclosure}"
- If they ask how we got their email, say truthfully: "{how_got}"
- End with "{first}".
Return JSON only: {{"body": "...", "answerable": true|false}}
Set answerable false if the question can't be answered from the facts and FAQ."""

GOALS = {
    "interested": "Thank them, explain in one or two sentences what happens next using the facts, and give the booking link.",
    "question": "Answer their question using only the facts and FAQ, then offer the link.",
    "not_now": "Acknowledge politely, say you won't follow up, no link needed.",
    "referral": "Thank them for pointing you to the right person. No link needed.",
}

CRITIC_SYSTEM = """You review one AI-written reply before it is sent to a prospect. Fail it if it claims anything
not in the offer facts/FAQ, makes a commitment, date or price promise, is pushy, ignores what they asked,
or pretends to be a person when asked. Return JSON only: {"pass": true|false, "issues": ["..."]}"""


def validate_reply(body: str, campaign: dict, inbound: str, settings: dict, how_got: str) -> list[str]:
    problems = []
    words = len(re.findall(r"\b[\w'/-]+\b", body.replace(LINK_TOKEN, "")))
    if not words or words > settings["writing"]["reply_max_words"]:
        problems.append(f"{words} words is outside 1-{settings['writing']['reply_max_words']}")
    if body.count(LINK_TOKEN) > 1:
        problems.append("link token more than once")
    if re.search(r"https?://|www\.", body.replace(LINK_TOKEN, ""), re.I):
        problems.append("extra link")
    if "$" in body or "!" in body or "—" in body or "–" in body:
        problems.append("price sign, exclamation or dash")
    allowed = set(re.findall(r"\d+", " ".join(campaign["offer"]["facts"] + [q["a"] for q in campaign["offer"].get("faq", [])]
                                              + [how_got])))
    stray = set(re.findall(r"\d+", body.replace(LINK_TOKEN, ""))) - allowed
    if stray:
        problems.append(f"numbers not in facts: {sorted(stray)}")
    if DATE_PROMISE.search(body):
        problems.append("dates, times or commitments")
    if any(b in body.lower() for b in BANNED):
        problems.append("banned phrase")
    if HUMAN_Q.search(inbound) and settings["replies"]["ai_disclosure"] not in body:
        problems.append("asked if a person is writing: the AI disclosure sentence is missing")
    return problems


def how_we_got(lead: dict | None) -> str:
    src = (lead or {}).get("email_source") or ""
    if src.startswith("site:"):
        return "I found it on the contact page of your website."
    if src.startswith("people:"):
        return f"It came from {src.split(':', 1)[1].title()}, a business contact directory."
    if src.startswith("referral:"):
        return "A colleague of yours suggested I reach out to you."
    return "I found it on your business's website."


FALLBACK = {
    "interested": "Thanks for getting back to me. Here is the link to pick a time that works for you: "
                  f"{LINK_TOKEN}\n\n{{first}}",
    "not_now": "Understood, thanks for letting me know. I won't follow up.\n\n{first}",
}


# ------------------------------------------------------------------ the agent
class ReplyAgent:
    def __init__(self, settings: dict, ledger, llm, *, pool: list[dict], hmac_key: str | None,
                 unsub_base: str, notifier=None, ghl_creds=None, imap_factory=None, smtp_factory=None):
        self.settings = settings
        self.ledger = ledger
        self.llm = llm
        self.pool = pool
        self.hmac_key = hmac_key
        self.unsub_base = unsub_base
        self.notifier = notifier
        self.ghl_creds = ghl_creds
        self.imap_factory = imap_factory or _imap_fetch
        self.smtp_factory = smtp_factory
        self.counts = {k: 0 for k in ("bounce", "auto", "optout", "unmatched", *CLASSES, "ai_replies",
                                      "escalated", "errors")}

    # --------------------------------------------------------------- polling
    def poll(self) -> dict:
        own = {m["email"] for m in self.pool}
        for mb in self.pool:
            try:
                messages = self.imap_factory(mb)
            except Exception as exc:  # one bad inbox must not stop the others
                self.counts["errors"] += 1
                self.ledger.event("inbox_error", f"Could not read mailbox {mb['id']}.",
                                  why=f"{type(exc).__name__}: {str(exc)[:150]}")
                continue
            for raw in messages:
                msg = email.message_from_bytes(raw) if isinstance(raw, (bytes, bytearray)) else raw
                if parseaddr(_hdr(msg, "From"))[1].lower() in own:
                    continue
                self.handle(mb, msg)
        return dict(self.counts)

    def handle(self, mb: dict, msg) -> str:
        mid = (msg.get("Message-ID") or "").strip()
        key = msgid_key(mid or (_hdr(msg, "From") + _hdr(msg, "Date") + _hdr(msg, "Subject")))
        # Claim first: a message that crashes mid-handling is never handled twice (no double replies).
        if not self.ledger.s.create("inbound", key, {"at": utcnow().isoformat(), "mailbox": mb["id"],
                                                     "status": "processing"}):
            return "seen"
        try:
            outcome = self._handle(mb, msg)
        except BudgetStop as exc:
            outcome = "escalated"
            self.counts["escalated"] += 1
            self.ledger.event("reply_escalated", "A reply needs you: the AI budget for replies is spent.",
                              why=str(exc), notify=True)
        self.ledger.s.set("inbound", key, {"status": "done", "outcome": outcome})
        return outcome

    def _match(self, msg) -> str | None:
        ids = re.findall(r"<[^>]+>", (msg.get("In-Reply-To") or "") + " " + (msg.get("References") or ""))
        for i in reversed(ids):
            tid = self.ledger.thread_for_message(i)
            if tid:
                return tid
        sender = parseaddr(_hdr(msg, "From"))[1].lower()
        ref = self.ledger.s.get("by_email", sender)
        if ref:
            return f"{ref.get('last_campaign')}__{ref['domain']}"
        return None

    def _handle(self, mb: dict, msg) -> str:
        kind, addr = rule_class(msg)
        if kind == "bounce":
            if not addr:
                self.counts["unmatched"] += 1
                return "bounce_unknown"
            self.ledger.suppress(addr, "hard bounce", "inbox")
            self._mark_bounce(addr)
            self.counts["bounce"] += 1
            return "bounce"
        tid = self._match(msg)
        thread = self.ledger.thread(tid) if tid else None
        if not thread:
            if kind == "optout" and addr:  # honor it even without a thread match
                self.ledger.suppress(addr, "replied opt-out", "inbox")
                self.counts["optout"] += 1
                return "optout"
            self.counts["unmatched"] += 1
            return "unmatched"
        c = self.ledger.campaign(thread["campaign_id"]) or {}
        lead = self.ledger.lead(thread["campaign_id"], thread["domain"]) or {}
        inbound_body = fresh_text(body_text(msg))
        thread["messages"] = thread["messages"] + [{"dir": "in", "message_id": msg.get("Message-ID"),
                                                    "at": utcnow().isoformat(), "from": addr,
                                                    "subject": _hdr(msg, "Subject"), "body": inbound_body[:4000]}]
        if kind == "optout":
            self.ledger.suppress(addr, "replied opt-out", "inbox")
            self._stop(thread, tid, c, lead, "optout")
            self.counts["optout"] += 1
            return "optout"
        if kind == "auto":
            until = add_business_days(today_et(), self.settings["replies"]["ooo_pause_business_days"])
            seq = dict(lead.get("seq") or {})
            seq["paused_until"] = until.isoformat()
            if seq.get("next_step") is not None:
                seq["next_due"] = max(str(seq.get("next_due") or ""), until.isoformat())
            self.ledger.save_lead(c["id"], lead["domain"], {"seq": seq})
            self.ledger.save_thread(tid, thread)
            self.counts["auto"] += 1
            return "auto"
        # ---- model classification
        try:
            verdict = self.llm.json("haiku", CLASSIFY_SYSTEM, f"Subject: {_hdr(msg, 'Subject')}\n\n{inbound_body[:3000]}",
                                    purpose="reply_classify", campaign_id=c.get("id"), max_tokens=800,
                                    schema=CLASSIFY_SCHEMA)
        except LLMError as exc:
            verdict = {"class": "unclear", "confidence": 0.0, "summary": f"classifier failed: {exc}"}
        klass = verdict.get("class") if verdict.get("class") in CLASSES else "unclear"
        conf = float(verdict.get("confidence") or 0)
        self.counts[klass] += 1
        thread["last_class"] = klass
        thread["messages"][-1].update(klass=klass, confidence=conf)
        cap = int((c.get("settings") or {}).get("ai_reply_cap", self.settings["replies"]["ai_reply_cap"]))
        # Any real reply stops the sequence.
        self._stop(thread, tid, c, lead, klass)
        if klass == "not_now":
            until = (date.today() + timedelta(days=self.settings["replies"]["not_now_days"])).isoformat()
            self.ledger.save_lead(c["id"], lead["domain"], {"status": "not_now", "not_now_until": until})
        escalate_why = None
        if klass not in AUTO_REPLY:
            escalate_why = f"classified {klass}"
        elif conf < self.settings["replies"]["min_confidence"]:
            escalate_why = f"classifier confidence {conf:.2f} is under {self.settings['replies']['min_confidence']}"
        elif thread.get("ai_replies", 0) >= cap:
            escalate_why = f"this thread already has {thread.get('ai_replies', 0)} AI replies (cap {cap})"
        if escalate_why:
            return self._escalate(thread, tid, c, lead, klass, inbound_body, escalate_why)
        body = self._write_reply(c, lead, klass, inbound_body)
        if body is None:
            return self._escalate(thread, tid, c, lead, klass, inbound_body,
                                  "the reply could not be written safely from the facts and FAQ")
        link = (c.get("booking_url") or c["landing_url"]) if klass == "interested" else campaign_link(c, lead["domain"], 99)
        try:
            send_reply(self.settings, self.ledger, mb, thread, tid, addr, body, link=link,
                       hmac_key=self.hmac_key, unsub_base=self.unsub_base,
                       in_reply_to=msg.get("Message-ID") or "", **({"smtp_factory": self.smtp_factory} if self.smtp_factory else {}))
        except (ComplianceError, OSError, Exception) as exc:  # noqa: BLE001 - any send failure escalates
            return self._escalate(thread, tid, c, lead, klass, inbound_body, f"AI reply failed to send: {type(exc).__name__}")
        thread = self.ledger.thread(tid)
        thread["ai_replies"] = thread.get("ai_replies", 0) + 1
        self.ledger.save_thread(tid, thread)
        self.counts["ai_replies"] += 1
        if klass == "interested":
            self.ledger.event("interested_reply", f"{lead.get('name') or thread['domain']} is interested.",
                              why="They replied with interest; the AI sent the booking link and stopped the sequence.",
                              cost_impact="none", campaign_id=c["id"], notify=True,
                              detail="Their reply:\n" + inbound_body[:1500] + "\n\nAI reply sent:\n" + body)
            if self.ghl_creds:
                from . import ghl
                ghl.upsert_contact(self.ghl_creds, {**lead, "email": addr}, self.settings["ghl"]["tags"] + ["leadgen-interested"])
        if klass == "referral":
            self._queue_referral(c, lead, verdict.get("referral_email") or "", addr)
        return klass

    # --------------------------------------------------------------- helpers
    def _stop(self, thread, tid, c, lead, why):
        """Any reply, opt-out or bounce ends the sequence for this lead."""
        seq = lead.get("seq") or {}
        if lead:
            self.ledger.save_lead(c["id"], lead["domain"], {
                "status": "suppressed" if why == "optout" else "replied", "reply_class": why,
                "replied_at": utcnow().isoformat(),
                "seq": {**seq, "next_step": None, "next_due": None, "stopped": why}})
        thread["state"] = "stopped" if why == "optout" else thread.get("state", "open")
        self.ledger.save_thread(tid, thread)

    def _mark_bounce(self, addr: str):
        ref = self.ledger.s.get("by_email", addr)
        if not ref:
            return
        for cid in ref.get("campaigns", []):
            lead = self.ledger.lead(cid, ref["domain"])
            if lead and lead.get("last_send_id"):
                self.ledger.s.set("sends", lead["last_send_id"], {"bounced": True})
            self.ledger.save_lead(cid, ref["domain"], {"status": "bounced"})

    def _write_reply(self, c, lead, klass, inbound) -> str | None:
        first = c["offer"].get("sender_name", "Marcus").split()[0]
        how = how_we_got(lead)
        facts = "\n".join(f"- {f}" for f in c["offer"]["facts"])
        faq = "\n".join(f"Q: {q['q']}\nA: {q['a']}" for q in c["offer"].get("faq", []))
        link_rule = (f"Put the token {LINK_TOKEN} once where the booking link goes." if klass == "interested"
                     else f"You may put the token {LINK_TOKEN} once for the offer page; no other links." if klass == "question"
                     else "No links.")
        system = REPLY_SYSTEM.format(sender=c["offer"].get("sender_name", "Marcus Barber"),
                                     company=c["offer"].get("sender_company", "Digital Solomon"),
                                     klass=klass, goal=GOALS[klass], max_words=self.settings["writing"]["reply_max_words"],
                                     link_rule=link_rule, disclosure=self.settings["replies"]["ai_disclosure"],
                                     how_got=how, first=first)
        user = f"Offer: {c['offer']['name']}\nFacts:\n{facts}\nFAQ:\n{faq or '(none)'}\n\nTheir reply:\n{inbound[:3000]}"
        problems = None
        for tier in ("haiku", "sonnet"):
            try:
                out = self.llm.json(tier, system + (f"\nYour last draft was rejected for: {'; '.join(problems)}." if problems else ""),
                                    user, purpose="reply_write" if tier == "haiku" else "reply_rewrite",
                                    campaign_id=c["id"], max_tokens=1500, schema=REPLY_SCHEMA)
            except LLMError:
                problems = ["model error"]
                continue
            if out.get("answerable") is False:
                return None
            body = (out.get("body") or "").strip()
            problems = validate_reply(body, c, inbound, self.settings, how)
            if klass == "interested" and LINK_TOKEN not in body:
                problems.append("interested reply needs the booking link")
            if not problems:
                verdict = self.llm.json("haiku", CRITIC_SYSTEM, f"Their reply:\n{inbound[:2000]}\n\nOur reply:\n{body}\n\nFacts:\n{facts}\nFAQ:\n{faq}",
                                        purpose="reply_critic", campaign_id=c["id"], max_tokens=800,
                                        schema=REPLY_CRITIC_SCHEMA)
                if verdict.get("pass") is True:
                    return body
                problems = ["critic: " + "; ".join(map(str, verdict.get("issues") or ["failed"]))]
        if klass in FALLBACK and not HUMAN_Q.search(inbound) and not HOW_GOT.search(inbound):
            return FALLBACK[klass].format(first=first)
        return None

    def _escalate(self, thread, tid, c, lead, klass, inbound, why) -> str:
        thread["state"] = "escalated"
        self.ledger.save_thread(tid, thread)
        suggestion = ""
        try:
            out = self.llm.json("haiku", "Suggest a short, honest reply Marcus could send. Use only the facts. "
                                "Return JSON only: {\"body\": \"...\"}",
                                f"Facts: {c.get('offer', {}).get('facts')}\nTheir reply:\n{inbound[:2000]}",
                                purpose="reply_write", campaign_id=c.get("id"), max_tokens=300)
            suggestion = out.get("body", "")
        except (LLMError, BudgetStop):
            suggestion = "(no suggestion: the AI budget or model was unavailable)"
        mb_email = next((m["email"] for m in self.pool if m["id"] == thread.get("mailbox")), "")
        gmail = f"https://mail.google.com/mail/u/?authuser={mb_email}#search/rfc822msgid%3A" + \
                (thread["messages"][-1].get("message_id") or "").strip("<>")
        self.ledger.event("reply_escalated", f"A reply from {lead.get('name') or thread['domain']} needs you ({klass}).",
                          why=why, cost_impact="none", campaign_id=c.get("id"), notify=True, thread_id=tid,
                          gmail_url=gmail,
                          detail="Their reply:\n" + inbound[:2000] + "\n\nSuggested reply:\n" + suggestion +
                          f"\n\nOpen in Gmail: {gmail}")
        self.counts["escalated"] += 1
        return "escalated"

    def _queue_referral(self, c, lead, referred: str, from_addr: str):
        referred = referred.strip().lower()
        if not referred or "@" not in referred:
            return
        if referred.split("@")[-1] != lead.get("domain") and not referred.endswith("." + lead.get("domain", "")):
            self.ledger.event("referral_skipped", f"Referral to {referred.split('@')[-1]} not queued.",
                              why="Referred address is not on the same business domain.", campaign_id=c["id"])
            return
        if self.ledger.is_suppressed(email=referred):
            return
        domain = lead["domain"]
        # Same business, new person: a fresh lead record under the referral suffix.
        key = f"{domain}~ref"
        self.ledger.save_lead(c["id"], key, {
            **{k: lead.get(k) for k in ("name", "segment", "geo", "cell_id", "website", "facts", "evidence",
                                        "tech", "social", "years_in_business", "maps", "brief")},
            "email": referred, "verdict": "unknown", "status": "referral_queued",
            "referred_by": from_addr, "email_source": f"referral:{from_addr}", "found_at": utcnow().isoformat()})
        self.ledger.event("referral_queued", f"Queued {referred} as a referral lead.",
                          why=f"{from_addr} pointed us to them.", campaign_id=c["id"])


def _imap_fetch(mb: dict, since_days: int = 3) -> list[bytes]:
    c = mb["creds"]
    conn = imaplib.IMAP4_SSL(c["imap_host"], int(c.get("imap_port", 993)))
    try:
        conn.login(c["user"], c["password"])
        conn.select("INBOX", readonly=True)
        since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        out = []
        for num in data[0].split():
            _, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if msg_data and msg_data[0]:
                out.append(msg_data[0][1])
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def recipients(msg) -> list[str]:
    return [a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))]
