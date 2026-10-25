"""Email 2: the preview delivery, in the same thread as Email 1. A template with slots, never model-written.

Slots: first name (the owner's, when the site named one), business name, the screenshot as the header
image, a button to the preview, the price (one sentence from the campaign's offer facts, chosen in
preview.price_fact), and a "Get started now" button to checkout. Then the standard full footer and
unsubscribe, the same as every sequence email.

Sent from the thread's own mailbox, Re: the thread subject, In-Reply-To the prospect's yes, References
the whole thread, BCC Marcus. Exactly once: a claim in leadgen_email2 is made before SMTP.

previews.email2_live is the human gate. While it is false, Email 2 is rendered, stored on the thread and
emailed to Marcus for review, and the prospect gets nothing.
"""
from __future__ import annotations

import html
import logging
import smtplib

from .compliance import ComplianceError
from .sender import build_outbound, stop_tag_hold, unsub_url
from .store import utcnow

log = logging.getLogger(__name__)

TEXT = """{greeting}

Here is the preview I built for {business}:
{preview_url}

{price}

If you want it, you can get started here:
{checkout_url}

{sender}"""

BUTTON = ('<a href="{url}" style="display:inline-block;background:{bg};color:#ffffff;text-decoration:none;'
          'font-weight:bold;padding:12px 22px;border-radius:6px">{label}</a>')

HTML = """<p>{greeting}</p>
<p>Here is the preview I built for {business}:</p>
<p><a href="{preview_url}"><img src="{screenshot_url}" width="560" alt="Preview of a new website for {business}"
 style="display:block;width:100%;max-width:560px;height:auto;border:1px solid #dddddd;border-radius:6px"></a></p>
<p>{preview_button}</p>
<p>{price}</p>
<p>{checkout_button}</p>
<p>{sender}</p>"""


def email2_live(settings: dict, ledger) -> bool:
    """The human gate: on when settings.yaml says so, or when Marcus flipped it with scripts/gate_email2.py
    (leadgen_state/previews.email2_live, read every send, so no redeploy is needed either way)."""
    return bool(settings["previews"].get("email2_live")) or bool(ledger.state("previews").get("email2_live"))


class Email2Error(ValueError):
    """Email 2 can't be rendered from what is stored (a missing slot). The thread is held."""


def price_sentence(c: dict) -> str:
    want = ((c.get("preview") or {}).get("price_fact") or "").strip()
    facts = [f.strip() for f in (c.get("offer") or {}).get("facts", [])]
    if not want:
        raise Email2Error("the campaign has no preview.price_fact")
    if want not in facts:
        raise Email2Error("preview.price_fact is not one of the campaign's offer facts")
    return want


def render(c: dict, lead: dict, item: dict, thread: dict) -> dict:
    """-> {subject, text, html}. Every word is either this template or a stored slot value."""
    for k in ("preview_url", "screenshot_url", "checkout_url"):
        if not item.get(k):
            raise Email2Error(f"{k} is missing")
    first = ((lead.get("owner") or {}).get("first_name") or "").strip()
    business = (lead.get("name") or "").strip() or "your business"
    sender = (c.get("offer") or {}).get("sender_name", "Marcus Barber").split()[0]
    slots = {"greeting": f"Hi {first}," if first else "Hi there,", "business": business,
             "price": price_sentence(c), "sender": sender, "preview_url": item["preview_url"],
             "checkout_url": item["checkout_url"]}
    esc = {k: html.escape(v, quote=True) for k, v in slots.items()}
    subject = thread["subject"] if thread["subject"].lower().startswith("re:") else "Re: " + thread["subject"]
    return {
        "subject": subject,
        "text": TEXT.format(**slots),
        "html": HTML.format(**esc, screenshot_url=html.escape(item["screenshot_url"], quote=True),
                            preview_button=BUTTON.format(url=esc["preview_url"], bg="#1f3a5f", label="See your preview"),
                            checkout_button=BUTTON.format(url=esc["checkout_url"], bg="#2e7d32", label="Get started now")),
    }


class Email2Sender:
    def __init__(self, settings: dict, ledger, *, pool: list[dict], hmac_key: str | None, unsub_base: str,
                 smtp_factory, notifier=None):
        self.settings = settings
        self.ledger = ledger
        self.pool = pool
        self.hmac_key = hmac_key
        self.unsub_base = unsub_base
        self.smtp_factory = smtp_factory
        self.notifier = notifier

    def send(self, tid: str, item: dict, stop_tags) -> tuple[str, str]:
        """-> (outcome, why). outcome: sent | review | wait | held | cancelled."""
        thread = self.ledger.thread(tid) or {}
        c = self.ledger.campaign(item["campaign_id"]) or {}
        lead = self.ledger.lead(item["campaign_id"], item["domain"]) or {}
        to = item.get("to") or thread.get("email")
        if self.ledger.is_suppressed(email=to, domain=item["domain"]):
            return "cancelled", "suppressed"
        held = stop_tag_hold(stop_tags, to, item.get("preview_id"))
        if held == "tagged":
            return "cancelled", "GHL stop tag"
        if held:
            return "wait", held
        if self.ledger.kill_switch_on():
            return "wait", "kill switch is on"
        try:
            mail = render(c, lead, item, thread)
        except Email2Error as exc:
            return "held", f"Email 2 could not be rendered: {exc}"
        if "/test_" in item.get("checkout_url", ""):
            # A preview built before the funnel went live keeps its test checkout (records keep what they
            # were built with). Never send a prospect a test link.
            return "held", "the preview's checkout link is a Stripe test link; ask the funnel to refresh it"
        if not email2_live(self.settings, self.ledger):
            return "review", mail["text"]
        mb = next((m for m in self.pool if m["id"] == thread.get("mailbox")), None)
        if not mb:
            return "held", "the thread's mailbox is not in the healthy pool"
        if not self.hmac_key or not self.unsub_base.startswith("https://"):
            return "wait", "unsubscribe signing key or service URL missing"
        refs = [m["message_id"] for m in thread.get("messages", []) if m.get("message_id")]
        try:
            msg = build_outbound(self.settings, mb, to, mail["subject"], mail["text"], link="",
                                 unsub_url=unsub_url(self.unsub_base, to, self.hmac_key), kind="sequence",
                                 in_reply_to=item.get("in_reply_to") or (refs[-1] if refs else None),
                                 references=refs, html_body=mail["html"])
        except ComplianceError as exc:
            return "held", f"compliance check failed: {exc}"
        # Exactly once: claim before SMTP; a crash after the claim is never resent.
        if not self.ledger.s.create("email2", tid, {"at": utcnow().isoformat(), "message_id": msg["Message-ID"],
                                                    "status": "sending"}):
            return "held", "Email 2 was already claimed for this thread"
        bcc = self.settings["replies"]["bcc"]
        try:
            with self.smtp_factory(mb["creds"]) as conn:
                refused = conn.send(msg, to_addrs=[to, bcc])
        except (smtplib.SMTPException, OSError) as exc:
            self.ledger.s.set("email2", tid, {"status": "unknown", "error": type(exc).__name__})
            return "held", f"SMTP failed mid-send ({type(exc).__name__}); not retried"
        if refused and to in refused:
            self.ledger.s.set("email2", tid, {"status": "refused"})
            self.ledger.suppress(to, "refused at send", "smtp")
            return "cancelled", "the address was refused at send"
        self.ledger.s.set("email2", tid, {"status": "sent"})
        thread = self.ledger.thread(tid) or thread
        thread["messages"] = thread.get("messages", []) + [{
            "dir": "out", "kind": "email2", "message_id": msg["Message-ID"], "at": utcnow().isoformat(),
            "subject": mail["subject"], "body": mail["text"], "bcc": bcc}]
        self.ledger.save_thread(tid, thread)
        self.ledger.index_message(msg["Message-ID"], tid)
        return "sent", msg["Message-ID"]
