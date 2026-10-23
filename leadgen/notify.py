"""Email notices to Marcus. Email only, no SMS.

Every event is written to leadgen_events (shown in the dashboard feed). Events
flagged notify=True are emailed at once by flush(). Each notice says what
changed, why, and the cost impact. Sender: the optional global-leadgen-notify-smtp
secret, else the first healthy pool mailbox (internal mail, not a prospect send).
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .mailboxes import SmtpConn
from .store import period_key, utcnow

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: dict, ledger, secrets, sender_creds=None, smtp_factory=SmtpConn):
        self.settings = settings
        self.ledger = ledger
        self.secrets = secrets
        self._sender_creds = sender_creds      # callable -> creds dict | None (first healthy mailbox)
        self.smtp_factory = smtp_factory
        self.sent: list[EmailMessage] = []

    def creds(self) -> dict | None:
        c = self.secrets.get_json(self.settings["notify"]["smtp_secret"])
        if c:
            return c
        return self._sender_creds() if self._sender_creds else None

    def email(self, subject: str, body: str) -> bool:
        creds = self.creds()
        if not creds:
            log.info("notice not emailed (no notify sender yet): %s", subject)
            return False
        sender = creds.get("email") or creds.get("user")
        msg = EmailMessage()
        msg["From"] = formataddr(("Solomon Outreach", sender))
        msg["To"] = self.settings["notify"]["to"]
        msg["Subject"] = subject[:150]
        msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body)
        try:
            with self.smtp_factory(creds) as s:
                s.send(msg)
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("notice email failed: %s", type(exc).__name__)
            return False
        self.sent.append(msg)
        return True

    def flush(self) -> int:
        """Email every pending notify event, oldest first. Idempotent."""
        pending = [e for e in self.ledger.events(limit=500) if e.get("notify") and not e.get("notified")]
        n = 0
        for e in sorted(pending, key=lambda r: r["at"]):
            body = "\n".join(filter(None, [
                f"What changed: {e['what']}",
                f"Why: {e['why']}" if e.get("why") else "",
                f"Cost impact: {e['cost_impact']}" if e.get("cost_impact") else "Cost impact: none",
                f"Campaign: {e['campaign_id']}" if e.get("campaign_id") else "",
                e.get("detail", ""),
                "", "Outreach dashboard: https://dashboard.example.com/#outreach",
            ]))
            if self.email(f"Outreach: {e['what'][:110]}", body):
                self.ledger.s.set("events", e["id"], {"notified": True, "notified_at": utcnow().isoformat()})
                n += 1
        return n

    def provider_exhausted(self, provider: str, reason: str, billing: bool, period: str = "month"):
        """Once per provider per period."""
        key = f"notified_exhausted_{provider}_{period_key(period)}"
        if self.ledger.state(key).get("at"):
            return
        self.ledger.set_state(key, at=utcnow().isoformat())
        if billing:
            self.ledger.event("provider_billing", f"{provider} disabled for the rest of the period.",
                              why=f"It returned a billing or overage signal: {reason[:200]}",
                              cost_impact="Stopped before any charge; $0 spent.", notify=True,
                              provider=provider)
        else:
            self.ledger.event("provider_exhausted", f"{provider} free allowance is spent for this period.",
                              why=reason[:200], cost_impact="None. The next free provider takes over; "
                              "if all are spent, that stage waits for the reset.",
                              notify=True, provider=provider)
