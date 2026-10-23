"""Write every email fresh, then let code and a second model decide what ships.

Fixed tier rule, per email:
  1. the campaign's writer tier (Haiku by default) writes the whole email from the
     lead brief, offer facts, the sequence step's angle and the campaign link token;
  2. the code validator (free) checks it;
  3. a Haiku critic (separate prompt and call) returns {pass, issues[]};
  4. on any failure Sonnet rewrites that one email once, and it goes through 2 and 3
     again. Still failing: first touches ship the pre-approved template; a follow-up
     is skipped.

Auto-escalation (campaign switches its writer from Haiku to Sonnet, never back by
itself): after 150+ first-touch sends the positive-reply rate is under target, or
more than 30% of the last 50 drafts failed the critic.
"""
from __future__ import annotations

import logging
import re

from .llm import STR, STRS, BudgetStop, LLMError, obj

log = logging.getLogger(__name__)

LINK_TOKEN = "{{campaign_link}}"
BANNED = ("guarantee", "risk-free", "risk free", "act now", "limited time", "urgent", "100%",
          "#1", "number one", "best in", "free money", "no obligation", "last chance",
          "don't miss", "dont miss", "skyrocket", "explode", "10x", "revolutionary",
          "game-changer", "game changer", "once in a lifetime", "exclusive deal", "hurry")


DRAFT_SCHEMA = obj({"subject": STR, "body": STR, "facts_used": STRS, "evidence_used": STRS})
CRITIC_SCHEMA = obj({"pass": {"type": "boolean"}, "issues": STRS})


class SkipEmail(Exception):
    """A follow-up that could not be written safely. The step is skipped, never forced."""


def fact_sheet(campaign: dict) -> dict[str, str]:
    return {f"offer_{i + 1}": t for i, t in enumerate(campaign["offer"]["facts"])}


def _words(body: str) -> int:
    return len(re.findall(r"\b[\w'/-]+\b", body.replace(LINK_TOKEN, "")))


def validate(draft: dict, campaign: dict, lead: dict, evidence: dict, step: int, *,
             max_words: int, min_words: int, first_subject: str | None = None) -> list[str]:
    """Validator v2. Pure code, free, and the last word on what can't be said."""
    problems = []
    subject = (draft.get("subject") or "").strip()
    body = (draft.get("body") or "").strip()
    used_ev = draft.get("evidence_used") or []
    used_facts = draft.get("facts_used") or []
    facts = fact_sheet(campaign)

    if step == 0:
        if not 4 <= len(subject) <= 70:
            problems.append("subject length")
    elif first_subject is not None and subject != first_subject:
        problems.append("follow-up must keep the first email's subject")
    if re.match(r"^\s*(re|fw|fwd)\s*:", subject, re.I):
        problems.append("subject pretends to be a reply/forward")
    if (subject.isupper() and len(subject) > 4) or "!" in subject or "!" in body:
        problems.append("shouting")
    lo = min_words if step == 0 else min(15, min_words)
    words = _words(body)
    if not lo <= words <= max_words:
        problems.append(f"body length {words} words (allowed {lo}-{max_words})")
    if body.count(LINK_TOKEN) != 1:
        problems.append("campaign link token must appear exactly once")
    if re.search(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|net|org|io|co)\b", subject + " " + body.replace(LINK_TOKEN, ""), re.I):
        problems.append("extra link")
    fact_text = " ".join(facts.values()).lower()
    price_words = {w.lower() for w in re.findall(r"\b(price|pricing|costs?|fees?|per month)\b", body, re.I)}
    if "$" in subject + body or any(w not in fact_text for w in price_words):
        problems.append("price talk")
    if "—" in body or "–" in body:
        problems.append("dashes")
    low = (subject + " " + body).lower()
    for b in BANNED:
        if b in low:
            problems.append(f"banned phrase {b!r}")
    allowed_text = " ".join(list(facts.values()) + list(evidence.values()) +
                            [lead.get("name", ""), lead.get("geo", "")])
    allowed_numbers = set(re.findall(r"\d+", allowed_text))
    stray = set(re.findall(r"\d+", subject + " " + body.replace(LINK_TOKEN, ""))) - allowed_numbers
    if stray:
        problems.append(f"numbers not in facts: {sorted(stray)}")
    unknown_f = [u for u in used_facts if u not in facts]
    if unknown_f:
        problems.append(f"unknown fact ids {unknown_f}")
    unknown_e = [u for u in used_ev if u not in evidence]
    if unknown_e:
        problems.append(f"unknown evidence keys {unknown_e}")
    if step == 0 and evidence and not set(used_ev) & set(evidence):
        problems.append("uses no evidence about the business")
    # Personal details must trace to the lead record.
    owner = (lead.get("owner") or {}).get("first_name")
    greet = re.match(r"^\s*(?:hi|hello|hey)\s+([^,\n]+)[,\n]", body, re.I)
    if greet:
        who = greet.group(1).strip()
        ok = {lead.get("name", "").lower(), "there", "team", (lead.get("name", "") + " team").lower()}
        if owner:
            ok.add(owner.lower())
        if who.lower() not in ok and not lead.get("name", "").lower().startswith(who.lower()):
            problems.append(f"greeting names someone not on the lead record ({who!r})")
    if owner and re.search(rf"\b{re.escape(owner)}\b", body) and "owner:first_name" not in used_ev:
        problems.append("uses the owner's name without citing owner:first_name")
    if re.search(r"\b(review|rating|stars?)\b", body, re.I) and not any(k.startswith(("maps:", "review:")) for k in used_ev):
        problems.append("mentions reviews or ratings without citing them")
    if lead.get("referred_by") and step == 0 and "referr" not in body.lower() and "suggested" not in body.lower() \
            and "pointed me" not in body.lower():
        problems.append("referral lead: first email must mention the referral")
    return problems


WRITER_SYSTEM = """You write one short, plain, first-person cold email from {sender} at {company} to a local business.
This is step {step_no} of {steps} in a sequence. Angle for this step: {angle}

Rules you must follow exactly:
- Use ONLY the offer facts and the EVIDENCE entries given. No other claims, numbers, statistics, prices, client names or results.
- Speak about their website only as what "I" saw or did not see. Never say the business is losing money or customers.
- Put the exact token {{{{campaign_link}}}} once where the link goes. No other links, URLs or domain names.
- No hype, no urgency, no flattery, no exclamation marks, no em or en dashes, no pressure.
- {subject_rule}
- Body: {min_words} to {max_words} words. Greet with "Hi" and {greeting}. End with the sender's first name only.
- Do not write a signature block, address, or unsubscribe text; the system adds those.
- Respect the brief's do_not_say list.
{extra}
Return JSON only: {{"subject": "...", "body": "...", "facts_used": ["offer fact ids"], "evidence_used": ["evidence keys you relied on"]}}"""

CRITIC_SYSTEM = """You are a strict reviewer of one cold email before it is sent. You get the email, the offer facts,
the EVIDENCE about the business, and the research brief.
Fail the email if ANY of these is true:
- a claim about the business is not supported by an EVIDENCE entry, or a claim about the offer is not in the offer facts;
- it ignores the brief or is not relevant to this business;
- the tone is pushy, salesy, flattering, alarmist, or applies pressure;
- it fabricates personalization (names, details, history) that is not in the EVIDENCE;
- it implies a prior relationship or conversation that did not happen.
Return JSON only: {"pass": true|false, "issues": ["short reason", ...]}"""

REWRITE_EXTRA = "Your job: rewrite a draft that was REJECTED for: {problems}. Fix every problem."


def _greeting(lead: dict) -> str:
    owner = (lead.get("owner") or {}).get("first_name")
    return (f"the owner's first name {owner} (cite owner:first_name)" if owner
            else f"the business name ({lead['name']})")


def _user_prompt(campaign: dict, lead: dict, evidence: dict, brief: dict, thread: list[dict]) -> str:
    facts = fact_sheet(campaign)
    lines = [f"Business: {lead['name']} ({lead.get('segment')}, {lead.get('geo')})",
             f"Offer: {campaign['offer']['name']}", "Offer facts (id: text):",
             *[f"- {k}: {v}" for k, v in facts.items()], "EVIDENCE (key: text):",
             *[f"- {k}: {v}" for k, v in evidence.items()],
             "Brief: " + str({k: brief.get(k) for k in ("pillar", "pillar_why", "strongest_observation",
                                                        "opening_angle", "do_not_say")})]
    if lead.get("referred_by"):
        lines.append(f"This person was suggested by {lead['referred_by']} at the same business; say so plainly.")
    for m in thread:
        lines.append(f"Earlier email in this thread (step {m['step']}), do not repeat it:\nSubject: "
                     f"{m['subject']}\n{m['body']}")
    return "\n".join(lines)


def template_first_touch(campaign: dict, lead: dict, evidence: dict) -> dict:
    """The fixed fallback. Every sentence is pre-approved and passes the validator."""
    order = ["fact:after_hours", "fact:no_chat", "fact:no_booking", "fact:no_form", "fact:no_schema"]
    key = next((k for k in order if k in evidence), None)
    first = campaign["offer"].get("sender_name", "Marcus").split()[0]
    observation = f"{evidence[key]} " if key else ""
    fact = campaign["offer"]["facts"][0] if campaign["offer"]["facts"] else ""
    seg = (lead.get("segment") or "local").lower()
    city = (lead.get("geo") or "").split(",")[0] or "your area"
    body = (f"Hi {lead['name']},\n\n"
            f"I took a look at your website while researching {seg} businesses around {city}. "
            f"{observation}\n\nThat is the kind of thing the {campaign['offer']['name']} looks at. {fact}\n\n"
            f"If it's useful, here it is: {LINK_TOKEN}\n\n{first}")
    return {"subject": f"A quick look at {lead['name'][:40]}'s website", "body": body,
            "facts_used": ["offer_1"] if fact else [], "evidence_used": [key] if key else [],
            "source": "template"}


class Writer:
    def __init__(self, llm, settings: dict, ledger):
        self.llm = llm
        self.cfg = settings["writing"]
        self.ledger = ledger

    def _model_draft(self, tier: str, campaign: dict, lead: dict, evidence: dict, brief: dict,
                     step: int, thread: list[dict], first_subject: str | None, problems=None) -> dict:
        seq = campaign["sequence"]
        angle = next((s["angle"] for s in seq if s["step"] == step), "")
        system = WRITER_SYSTEM.format(
            sender=campaign["offer"].get("sender_name", "Marcus Barber"),
            company=campaign["offer"].get("sender_company", "Digital Solomon"),
            step_no=step + 1, steps=len(seq), angle=angle,
            subject_rule=("Subject line: under 60 characters, plain, honest, not \"Re:\" or \"Fwd:\"."
                          if step == 0 else f"Subject line: exactly \"{first_subject}\" (same thread)."),
            min_words=self.cfg["min_words"] if step == 0 else 15,
            max_words=self.cfg["max_words"] - 10 if step == 0 else 80,
            greeting=_greeting(lead),
            extra=REWRITE_EXTRA.format(problems="; ".join(problems)) if problems else "")
        user = _user_prompt(campaign, lead, evidence, brief, thread)
        purpose = "rewrite" if problems else ("first_touch" if step == 0 else "follow_up")
        draft = self.llm.json(tier, system, user, purpose=purpose, campaign_id=campaign["id"],
                              max_tokens=2000, schema=DRAFT_SCHEMA)
        draft["subject"] = (first_subject if step > 0 and first_subject else draft.get("subject", "")).strip()
        draft["body"] = (draft.get("body") or "").strip()
        return draft

    def critic(self, campaign: dict, draft: dict, evidence: dict, brief: dict) -> dict:
        user = "\n".join([f"Subject: {draft['subject']}", draft["body"], "", "Offer facts:",
                          *[f"- {f}" for f in campaign["offer"]["facts"]], "EVIDENCE:",
                          *[f"- {k}: {v}" for k, v in evidence.items()], "Brief: " + str(brief)])
        out = self.llm.json("haiku", CRITIC_SYSTEM, user, purpose="critic",
                            campaign_id=campaign["id"], max_tokens=1000, schema=CRITIC_SCHEMA)
        return {"pass": out.get("pass") is True, "issues": [str(i)[:200] for i in out.get("issues") or []]}

    def _check(self, draft, campaign, lead, evidence, brief, step, first_subject):
        problems = validate(draft, campaign, lead, evidence, step, max_words=self.cfg["max_words"],
                            min_words=self.cfg["min_words"], first_subject=first_subject)
        if problems:
            return problems, None
        verdict = self.critic(campaign, draft, evidence, brief)
        return ([] if verdict["pass"] else ["critic: " + "; ".join(verdict["issues"] or ["failed"])]), verdict

    def write(self, campaign: dict, lead: dict, evidence: dict, brief: dict, step: int = 0,
              thread: list[dict] | None = None) -> dict:
        """Return {subject, body, source, tier, ...}. Raises SkipEmail for a follow-up that fails.

        BudgetStop propagates: the caller stops writing rather than template-blasting.
        """
        thread = thread or []
        first_subject = thread[0]["subject"] if thread else None
        tier = (campaign.get("settings") or {}).get("writer_tier", "haiku")
        attempts = []
        if self.llm and self.llm.available:
            for t, prior in ((tier, None), ("sonnet", "rewrite")):
                try:
                    draft = self._model_draft(t, campaign, lead, evidence, brief, step, thread,
                                              first_subject, problems=attempts[-1] if prior else None)
                except LLMError as exc:
                    attempts.append([f"model error: {exc}"])
                    continue
                problems, verdict = self._check(draft, campaign, lead, evidence, brief, step, first_subject)
                if prior is None and verdict is not None:
                    self._record_critic(campaign["id"], verdict["pass"])
                if not problems:
                    draft.update(source=f"model-{t}" if prior is None else "sonnet-rewrite", tier=t,
                                 critic=verdict, attempts=attempts)
                    return draft
                attempts.append(problems)
                log.info("draft rejected (%s step %d, %s): %s", lead["domain"], step, t, problems)
        if step == 0:
            out = template_first_touch(campaign, lead, evidence)
            out["attempts"] = attempts
            return out
        raise SkipEmail("; ".join(sum(attempts, [])) or "no writer available")

    def _record_critic(self, campaign_id: str, passed: bool):
        key = f"writer_stats_{campaign_id}"
        st = self.ledger.state(key)
        window = (st.get("critic_window") or [])[-(self.cfg["escalate_critic_fail_window"] - 1):]
        window.append(bool(passed))
        self.ledger.set_state(key, critic_window=window)


def escalation_reason(settings: dict, ledger, campaign: dict, first_touch_sends: int,
                      positives: int) -> str | None:
    """Why this campaign's writer should switch up to Sonnet, or None. Never switches down."""
    cfg = settings["writing"]
    s = campaign.get("settings") or {}
    if s.get("writer_tier") == "sonnet":
        return None
    target = s.get("positive_reply_target", cfg["escalate_positive_rate_below"])
    if first_touch_sends >= cfg["escalate_after_first_touches"]:
        rate = positives / first_touch_sends
        if rate < target:
            return (f"positive-reply rate is {rate:.2%} after {first_touch_sends} first touches, "
                    f"under the {target:.2%} target")
    window = ledger.state(f"writer_stats_{campaign['id']}").get("critic_window") or []
    if len(window) >= cfg["escalate_critic_fail_window"]:
        fail = round((len(window) - sum(window)) / len(window), 6)
        if fail > cfg["escalate_critic_fail_rate"]:
            return f"{fail:.0%} of the last {len(window)} drafts failed the critic"
    return None
