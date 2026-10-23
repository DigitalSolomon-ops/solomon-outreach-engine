"""CAN-SPAM floor, enforced in code on every outgoing message, never by the model.

Checked here, not remembered: accurate From, honest subject (validator in
writer.py), advertisement identification, valid postal address, a working
opt-out (link + reply), and RFC 8058 one-click unsubscribe headers. Sequence
follow-ups keep the full footer; AI replies keep a one-line footer with the link.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

AD_LINE = "This is a business introduction and advertisement from {company}."


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_token(email: str, secret: str) -> str:
    """Token = email + HMAC, so the unsubscribe service needs no lookup and can't be forged."""
    payload = email.strip().lower().encode()
    mac = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:16]
    return f"{_b64(payload)}.{_b64(mac)}"


def verify_token(token: str, secret: str) -> str | None:
    try:
        p, m = token.split(".", 1)
        payload = _unb64(p)
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:16]
        if hmac.compare_digest(expected, _unb64(m)):
            return payload.decode()
    except (ValueError, UnicodeDecodeError):
        pass
    return None


def footer_text(company: str, postal: str, unsub_url: str) -> str:
    return (f"\n\n--\n{AD_LINE.format(company=company)}\n{postal}\n"
            f"If you'd rather not hear from us, unsubscribe here: {unsub_url}\n"
            "or reply with the word remove.")


def footer_html(company: str, postal: str, unsub_url: str) -> str:
    return (f'<p style="color:#6b6b6b;font-size:12px;margin-top:24px">'
            f"{AD_LINE.format(company=company)}<br>{postal}<br>"
            f'If you\'d rather not hear from us, <a href="{unsub_url}">unsubscribe here</a> '
            "or reply with the word remove.</p>")


def reply_footer_text(unsub_url: str) -> str:
    return f"\n\n--\nTo stop these emails: {unsub_url} or reply remove."


def reply_footer_html(unsub_url: str) -> str:
    return (f'<p style="color:#6b6b6b;font-size:12px;margin-top:20px">To stop these emails: '
            f'<a href="{unsub_url}">unsubscribe</a> or reply remove.</p>')


class ComplianceError(Exception):
    pass


def preflight(msg_text: str, headers: dict, postal: str, unsub_url: str, from_email: str,
              kind: str = "sequence"):
    """Last gate before SMTP. Raises instead of sending anything non-compliant.

    kind: "sequence" (first touch or follow-up: full footer) | "reply" (one-line footer).
    """
    problems = []
    if kind == "sequence":
        if postal not in msg_text:
            problems.append("postal address missing")
        if "advertisement" not in msg_text.lower():
            problems.append("advertisement identification missing")
    if not unsub_url or unsub_url not in msg_text:
        problems.append("unsubscribe link missing")
    if "List-Unsubscribe" not in headers or "List-Unsubscribe-Post" not in headers:
        problems.append("one-click unsubscribe headers missing")
    if not from_email or from_email.lower() not in headers.get("From", "").lower():
        problems.append("From header does not match the sending mailbox")
    if "CHANGE-ME" in (unsub_url + from_email) or not unsub_url.startswith("https://"):
        problems.append("placeholder sender or unsubscribe URL still configured")
    if "{{" in msg_text:
        problems.append("unfilled template token")
    if problems:
        raise ComplianceError("; ".join(problems))
