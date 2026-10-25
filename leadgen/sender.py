"""Outbound mail: message building, the sequence clock, and the exactly-once tick sender.

Exactly-once: the send record {id: campaign:lead:step, status: sending} is created
BEFORE SMTP and marked sent after. A sending record older than 10 minutes becomes
unknown and is never retried. A Firestore lease stops two ticks overlapping.

Every individual send re-checks the global kill switch, the campaign's status, suppression and
the GHL stop tags (presence:booked / presence:customer); a stop-tag check that can't be made holds
the send.
Mailboxes ramp 5/day +2/day to 30; the campaign's daily cap applies on top; sends
are spread across the tick runs inside 9:00-16:00 ET on business days.
"""
from __future__ import annotations

import html
import logging
import math
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .campaigns import SENDING_STATUSES, campaign_link, offers_preview
from .compliance import (ComplianceError, footer_html, footer_text, preflight, reply_footer_html,
                         reply_footer_text, sign_token)
from .mailboxes import ET, SmtpConn, et_day_start, in_window, ticks_left_today, warmup_cap
from .stoptags import StopTagUnavailable
from .store import send_id, utcnow
from .writer import LINK_TOKEN

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ clock
def business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def due_date(first_touch: date, day_offset: int) -> date:
    return business_day(first_touch + timedelta(days=day_offset))


def today_et(now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone(ET).date()


# ------------------------------------------------------------------ messages
def _html_paragraphs(text: str, link: str | None) -> str:
    out = []
    for p in text.split("\n\n"):
        esc = html.escape(p).replace("\n", "<br>")
        if link:
            esc = esc.replace(html.escape(link), f'<a href="{html.escape(link)}">{html.escape(link)}</a>')
        out.append(f"<p>{esc}</p>")
    return "".join(out)


def build_outbound(settings: dict, mailbox: dict, to_email: str, subject: str, body: str, *,
                   link: str, unsub_url: str, kind: str = "sequence", in_reply_to: str | None = None,
                   references: list[str] | None = None, html_body: str | None = None,
                   plain_only: bool = False) -> EmailMessage:
    """kind: sequence (first touch / follow-up, full footer) | reply (one-line footer).
    html_body: a pre-built HTML body (Email 2's template) used instead of the plain paragraphs.
    plain_only: text/plain only, no HTML part (preview campaigns' Email 1 and follow-ups)."""
    cfg = settings["send"]
    creds = mailbox["creds"]
    from_email = creds["email"]
    filled = body.replace(LINK_TOKEN, link) if link else body
    if kind == "sequence":
        text = filled + footer_text(cfg["company"], cfg["postal_address"], unsub_url)
        foot = footer_html(cfg["company"], cfg["postal_address"], unsub_url)
    else:
        text = filled + reply_footer_text(unsub_url)
        foot = reply_footer_html(unsub_url)
    msg = EmailMessage()
    msg["From"] = formataddr((creds.get("from_name") or "Marcus Barber", from_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = from_email
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join((references or []) + ([] if in_reply_to in (references or []) else [in_reply_to]))
    mailto = from_email
    msg["List-Unsubscribe"] = f"<{unsub_url}>, <mailto:{mailto}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    if not plain_only:
        msg.add_alternative('<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5">'
                            f"{html_body if html_body is not None else _html_paragraphs(filled, link)}{foot}</div>",
                            subtype="html")
    preflight(text, dict(msg.items()), cfg["postal_address"], unsub_url, from_email, kind=kind)
    # Bcc is never a header: the caller passes it to SMTP as an envelope recipient.
    return msg


def unsub_url(base: str, email: str, hmac_key: str) -> str:
    return base.rstrip("/") + "/u/" + sign_token(email, hmac_key)


# ------------------------------------------------------------------ tick sender
class TickSender:
    def __init__(self, settings: dict, ledger, *, pool: list[dict], hmac_key: str | None,
                 unsub_base: str, stop_tags=None, smtp_factory=SmtpConn, now=utcnow):
        self.settings = settings
        self.stop_tags = stop_tags        # StopTags; None holds every send
        self.cfg = settings["send"]
        self.ledger = ledger
        self.pool = pool                  # healthy, active mailboxes with creds
        self.hmac_key = hmac_key
        self.unsub_base = unsub_base
        self.smtp_factory = smtp_factory
        self.now = now

    def _sent_today(self, **kw) -> list[dict]:
        return self.ledger.sends_since(et_day_start(self.now()), **kw)

    def mailbox_remaining(self, mb: dict) -> int:
        first = self.ledger.first_send_at(mb["id"])
        return max(0, warmup_cap(self.cfg, first, self.now()) - len(self._sent_today(mailbox=mb["id"])))

    def campaign_remaining(self, c: dict) -> int:
        sent = len(self._sent_today(campaign_id=c["id"]))
        cap = int((c.get("settings") or {}).get("daily_cap", 30))
        if c["status"] == "testing":
            batch = int((c.get("settings") or {}).get("test_batch_size", self.cfg["test_batch_size"]))
            tested = len([s for _, s in self.ledger.s.where("sends", ("campaign_id", "==", c["id"]))
                          if s.get("step") == 0 and s.get("status") in ("sent", "unknown", "sending")])
            return max(0, min(cap - sent, batch - tested))
        return max(0, cap - sent)

    def due(self, c: dict) -> list[dict]:
        today = today_et(self.now()).isoformat()
        out = []
        for lead in self.ledger.leads(c["id"], "in_sequence"):
            seq = lead.get("seq") or {}
            step = seq.get("next_step")
            if step is None or str(seq.get("next_due", "9999")) > today:
                continue
            if str(seq.get("paused_until", "")) > today:
                continue
            if str(step) not in (seq.get("drafts") or {}):
                continue
            out.append(lead)
        # first touches only after due follow-ups, so live threads finish first
        return sorted(out, key=lambda l: (l["seq"]["next_step"] == 0, l["seq"].get("next_due", "")))

    def run(self) -> dict:
        out = {"sent": 0, "status": "", "skipped": {}, "per_campaign": {}}
        ok, why = in_window(self.cfg, self.now())
        if not ok:
            out["status"] = f"held: {why}"
            return out
        if self.ledger.kill_switch_on():
            out["status"] = "stopped: kill switch is on"
            return out
        if not self.pool:
            out["status"] = "held: no healthy mailbox"
            return out
        if not self.hmac_key or not self.unsub_base.startswith("https://"):
            out["status"] = "held: unsubscribe signing key or service URL missing"
            return out
        campaigns = self.ledger.campaigns(*SENDING_STATUSES)
        work = []
        for c in campaigns:
            rem = self.campaign_remaining(c)
            if rem <= 0:
                continue
            for lead in self.due(c)[:rem]:
                work.append((c, lead))
        if not work:
            out["status"] = "nothing due"
            return out
        mb_rem = {mb["id"]: self.mailbox_remaining(mb) for mb in self.pool}
        capacity_today = sum(mb_rem.values()) + len(self._sent_today())
        ticks = ticks_left_today(self.cfg, self.now())
        this_tick = max(1, math.ceil(min(len(work), sum(mb_rem.values())) / ticks))
        mx_today = len([s for s in self._sent_today() if s.get("verdict") == "mx_only"])
        mx_cap = math.floor(self.cfg["mx_only_max_share"] * max(capacity_today, 1))
        conns: dict[str, object] = {}
        try:
            for c, lead in work:
                if out["sent"] >= this_tick:
                    break
                # Re-check before EVERY send, not once per run.
                if self.ledger.kill_switch_on():
                    out["status"] = "stopped: kill switch turned on mid-run"
                    break
                live = self.ledger.campaign(c["id"]) or {}
                if live.get("status") not in SENDING_STATUSES:
                    out["skipped"][c["id"]] = f"campaign is {live.get('status')}"
                    continue
                if self.ledger.is_suppressed(email=lead["email"], domain=lead["domain"]):
                    self.ledger.save_lead(c["id"], lead["domain"], {"status": "suppressed"})
                    continue
                held = stop_tag_hold(self.stop_tags, lead["email"], (lead.get("preview") or {}).get("preview_id"))
                if held == "tagged":
                    self.ledger.save_lead(c["id"], lead["domain"], {"status": "suppressed"})
                    continue
                if held:
                    out["skipped"][lead["domain"]] = held
                    continue
                if lead.get("verdict") == "mx_only" and lead["seq"]["next_step"] == 0:
                    if mx_today + 1 > mx_cap:
                        out["skipped"][lead["domain"]] = "mx_only share for today is used"
                        continue
                mb = self._mailbox_for(lead, mb_rem)
                if not mb:
                    out["skipped"][lead["domain"]] = "no mailbox with room (or its thread mailbox is retired)"
                    continue
                res = self._send_one(live, lead, mb, conns)
                if res == "sent":
                    out["sent"] += 1
                    mb_rem[mb["id"]] -= 1
                    out["per_campaign"][c["id"]] = out["per_campaign"].get(c["id"], 0) + 1
                    if lead.get("verdict") == "mx_only" and lead["seq"]["next_step"] == 0:
                        mx_today += 1
        finally:
            for conn in conns.values():
                conn.__exit__(None, None, None)
        out["status"] = out["status"] or "sent"
        return out

    def _mailbox_for(self, lead: dict, mb_rem: dict) -> dict | None:
        seq = lead["seq"]
        if seq.get("mailbox"):  # follow-ups thread from the same mailbox
            mb = next((m for m in self.pool if m["id"] == seq["mailbox"]), None)
            return mb if mb and mb_rem.get(mb["id"], 0) > 0 else None
        free = [m for m in self.pool if mb_rem.get(m["id"], 0) > 0]
        return max(free, key=lambda m: mb_rem[m["id"]]) if free else None

    def _conn(self, mb: dict, conns: dict):
        if mb["id"] not in conns:
            conns[mb["id"]] = self.smtp_factory(mb["creds"]).__enter__()
        return conns[mb["id"]]

    def _send_one(self, c: dict, lead: dict, mb: dict, conns: dict) -> str:
        seq = lead["seq"]
        step = int(seq["next_step"])
        draft = seq["drafts"][str(step)]
        sid = send_id(c["id"], lead["domain"], step)
        link = campaign_link(c, lead["domain"], step)
        try:
            msg = build_outbound(self.settings, mb, lead["email"], draft["subject"], draft["body"],
                                 link=link, unsub_url=unsub_url(self.unsub_base, lead["email"], self.hmac_key),
                                 in_reply_to=seq.get("last_message_id") if step else None,
                                 references=seq.get("references") if step else None,
                                 plain_only=offers_preview(c))
        except ComplianceError as exc:
            self.ledger.save_lead(c["id"], lead["domain"], {"status": "held_compliance", "error": str(exc)})
            self.ledger.event("compliance_hold", f"Held a send to {lead['domain']}.", why=str(exc),
                              campaign_id=c["id"])
            return "held"
        if not self.ledger.begin_send(sid, {"campaign_id": c["id"], "domain": lead["domain"], "step": step,
                                            "mailbox": mb["id"], "verdict": lead.get("verdict"),
                                            "message_id": msg["Message-ID"]}):
            return "already"  # sent, sending, or unknown: never twice
        try:
            refused = self._conn(mb, conns).send(msg)
        except smtplib.SMTPRecipientsRefused:
            refused = {lead["email"]: "refused"}
        except (smtplib.SMTPException, OSError) as exc:
            # Connection-level failure: the outcome is unknown. Leave the claim as
            # "sending"; it expires to "unknown" and is never retried.
            log.warning("SMTP failure mid-send (%s): %s", mb["id"], type(exc).__name__)
            conns.pop(mb["id"], None)
            return "unknown"
        if refused:
            self.ledger.finish_send(sid, "refused", bounced=True)
            self.ledger.suppress(lead["email"], "refused at send", "smtp")
            self.ledger.save_lead(c["id"], lead["domain"], {"status": "bounced"})
            return "refused"
        self.ledger.finish_send(sid, "sent", bounced=False)
        now = self.now()
        if not self.ledger.first_send_at(mb["id"]):
            self.ledger.s.set("mailboxes", mb["id"], {"first_send_at": now.isoformat()})
        tid = seq.get("thread_id") or f"{c['id']}__{lead['domain']}"
        refs = (seq.get("references") or []) + [msg["Message-ID"]]
        thread = self.ledger.thread(tid) or {"campaign_id": c["id"], "domain": lead["domain"],
                                             "email": lead["email"], "mailbox": mb["id"],
                                             "subject": draft["subject"], "messages": [],
                                             "ai_replies": 0, "state": "open"}
        thread["messages"] = thread["messages"] + [{"dir": "out", "kind": "sequence", "step": step,
                                                    "message_id": msg["Message-ID"], "at": now.isoformat(),
                                                    "subject": draft["subject"], "body": draft["body"]}]
        self.ledger.save_thread(tid, thread)
        self.ledger.index_message(msg["Message-ID"], tid)
        self.ledger.index_email(lead["email"], c["id"], lead["domain"])
        nxt = step + 1
        steps = {s["step"]: s for s in c["sequence"]}
        first_day = date.fromisoformat(seq.get("first_touch_date") or today_et(now).isoformat())
        if step == 0:
            first_day = today_et(now)
        update = {"seq": {**seq, "mailbox": mb["id"], "thread_id": tid, "references": refs,
                          "last_message_id": msg["Message-ID"], "first_touch_date": first_day.isoformat(),
                          "sent_steps": sorted(set(seq.get("sent_steps", [])) | {step}),
                          "first_subject": seq.get("first_subject") or draft["subject"]},
                  "contacted_at": now.isoformat(), "last_send_id": sid}
        if nxt in steps:
            update["seq"].update(next_step=nxt, next_due=due_date(first_day, steps[nxt]["day"]).isoformat())
        else:
            update["seq"].update(next_step=None, next_due=None)
            update["status"] = "sequence_done"
        self.ledger.save_lead(c["id"], lead["domain"], update)
        return "sent"


def stop_tag_hold(stop_tags, email: str, preview_id: str | None = None) -> str | None:
    """None = clear to send; "tagged" = stop tag found (now suppressed); else why the send is held.
    preview_id: once a lead has a preview, a contact carrying that PW Preview ID can stop it too."""
    if stop_tags is None:
        return "held: stop-tag check not configured"
    try:
        return "tagged" if stop_tags.check(email, preview_id) else None
    except StopTagUnavailable as exc:
        return f"held: stop-tag check unavailable ({exc})"


def send_reply(settings: dict, ledger, mb: dict, thread: dict, tid: str, to_email: str, body: str, *,
               link: str, hmac_key: str, unsub_base: str, in_reply_to: str, smtp_factory=SmtpConn,
               kind: str = "ai_reply") -> str:
    """Send one AI reply threaded from the same mailbox, BCC Marcus. Returns the Message-ID."""
    subject = thread["subject"] if thread["subject"].lower().startswith("re:") else "Re: " + thread["subject"]
    refs = [m["message_id"] for m in thread["messages"] if m.get("message_id")]
    bcc = settings["replies"]["bcc"]
    msg = build_outbound(settings, mb, to_email, subject, body, link=link,
                         unsub_url=unsub_url(unsub_base, to_email, hmac_key), kind="reply",
                         in_reply_to=in_reply_to, references=refs)
    with smtp_factory(mb["creds"]) as conn:
        conn.send(msg, to_addrs=[to_email, bcc])
    now = utcnow().isoformat()
    thread["messages"] = thread["messages"] + [{"dir": "out", "kind": kind, "message_id": msg["Message-ID"],
                                                "at": now, "subject": subject, "body": body, "bcc": bcc}]
    ledger.save_thread(tid, thread)
    ledger.index_message(msg["Message-ID"], tid)
    return msg["Message-ID"]
