"""Sending mailbox pool: one secret per mailbox, health checks, warmup, retirement.

Each mailbox is a secret global-leadgen-mailbox-<n> holding JSON
  {email, from_name, smtp_host, smtp_port, imap_host, user, password}
(optional: imap_port, dkim_selector). Any SMTP/IMAP provider works.

A mailbox sends only when `health` has confirmed its domain publishes SPF, a
DKIM key, and DMARC, and is not on the Spamhaus DBL. Spamhaus "public resolver
/ refused" codes (127.255.255.x) are UNKNOWN, not listed.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from .store import utcnow

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
REQUIRED = {"email", "smtp_host", "smtp_port", "imap_host", "user", "password"}
DKIM_SELECTORS = ("google", "default", "selector1", "selector2", "k1", "k2", "s1", "s2", "mail",
                  "dkim", "smtp", "zoho", "protonmail", "fm1", "mxvault", "titan1")


def mailbox_id(secret_name: str) -> str:
    return secret_name.rsplit("-", 1)[-1] if secret_name[-1].isdigit() else secret_name


def load_pool(secrets, names: list[str]) -> list[dict]:
    """Read each mailbox secret. A malformed one is reported, never fatal."""
    pool = []
    for name in sorted(names):
        try:
            creds = secrets.get_json(name)
        except ValueError:
            pool.append({"id": mailbox_id(name), "secret": name, "error": "secret is not valid JSON"})
            continue
        if not creds:
            continue
        missing = REQUIRED - set(creds)
        if missing:
            pool.append({"id": mailbox_id(name), "secret": name,
                         "error": f"secret is missing {sorted(missing)}"})
            continue
        pool.append({"id": mailbox_id(name), "secret": name, "creds": creds,
                     "email": creds["email"].lower(), "domain": creds["email"].split("@")[-1].lower()})
    return pool


# --------------------------------------------------------------- DNS health
def _txt(resolver, name: str) -> list[str]:
    import dns.exception
    import dns.resolver
    try:
        ans = resolver.resolve(name, "TXT", lifetime=10)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException:
        return ["__error__"]
    return [b"".join(r.strings).decode("utf-8", "replace") for r in ans]


def spamhaus_dbl(domain: str, resolver) -> str:
    """listed | clean | unknown. 127.255.255.x answers are Spamhaus refusing the query."""
    import dns.exception
    import dns.resolver
    try:
        ans = resolver.resolve(f"{domain}.dbl.spamhaus.org", "A", lifetime=10)
    except dns.resolver.NXDOMAIN:
        return "clean"
    except (dns.resolver.NoAnswer, dns.exception.DNSException):
        return "unknown"
    codes = [r.to_text() for r in ans]
    if all(c.startswith("127.255.255.") for c in codes):
        return "unknown"
    if any(c.startswith("127.0.1.") for c in codes):
        return "listed"
    return "unknown"


def domain_health(domain: str, dkim_selector: str | None = None, resolver=None) -> dict:
    if resolver is None:
        import dns.resolver
        resolver = dns.resolver.Resolver()
    spf = any(t.lower().startswith("v=spf1") for t in _txt(resolver, domain))
    dmarc = any(t.lower().startswith("v=dmarc1") for t in _txt(resolver, f"_dmarc.{domain}"))
    dkim, selector = False, None
    for sel in ([dkim_selector] if dkim_selector else []) + list(DKIM_SELECTORS):
        recs = _txt(resolver, f"{sel}._domainkey.{domain}")
        if any("p=" in t for t in recs if t != "__error__"):
            dkim, selector = True, sel
            break
    dbl = spamhaus_dbl(domain, resolver)
    ok = spf and dkim and dmarc and dbl != "listed"
    return {"spf": spf, "dkim": dkim, "dkim_selector": selector, "dmarc": dmarc, "dbl": dbl,
            "ok": ok, "checked_at": utcnow().isoformat()}


# --------------------------------------------------------------- warmup
def warmup_cap(cfg: dict, first_send_at: datetime | None, now: datetime | None = None) -> int:
    now = now or utcnow()
    days = (now.astimezone(ET).date() - first_send_at.astimezone(ET).date()).days if first_send_at else 0
    return min(cfg["warmup_max"], cfg["warmup_start"] + cfg["warmup_ramp_per_day"] * max(0, days))


def et_day_start(now: datetime | None = None) -> datetime:
    now = (now or utcnow()).astimezone(ET)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))


def in_window(cfg: dict, now: datetime | None = None) -> tuple[bool, str]:
    now = (now or utcnow()).astimezone(ET)
    if cfg.get("weekdays_only", True) and now.weekday() >= 5:
        return False, "weekend"
    start, end = cfg.get("window_et", [9, 16])
    if not start <= now.hour < end:
        return False, f"outside {start}:00-{end}:00 ET"
    return True, ""


def ticks_left_today(cfg: dict, now: datetime | None = None) -> int:
    """How many tick runs remain in today's window, counting this one."""
    now = (now or utcnow()).astimezone(ET)
    end = now.replace(hour=cfg["window_et"][1], minute=0, second=0, microsecond=0)
    minutes = max(0, (end - now).total_seconds() / 60)
    return max(1, int(minutes // cfg.get("tick_minutes", 10)) + 1)


def bounce_rate(sends: list[dict]) -> tuple[float, int, int]:
    n = len(sends)
    b = sum(1 for s in sends if s.get("bounced"))
    return (b / n if n else 0.0), b, n


def mailbox_retire_check(cfg: dict, ledger, mb_id: str, health: dict | None) -> str | None:
    """Reason to retire this mailbox, or None. Others keep sending."""
    if health and health.get("dbl") == "listed":
        return "its domain is listed on the Spamhaus DBL"
    sends = ledger.sends_since(utcnow() - timedelta(days=7), mailbox=mb_id)
    rate, b, n = bounce_rate(sends)
    if n >= cfg["mailbox_retire_bounce_min"] and rate > cfg["mailbox_retire_bounce_rate"]:
        return f"its 7-day bounce rate is {rate:.1%} ({b}/{n})"
    return None


# --------------------------------------------------------------- SMTP
class SmtpConn:
    """One authenticated SMTP session for a mailbox (or the notify sender)."""

    def __init__(self, creds: dict):
        self.creds = creds
        self._conn: smtplib.SMTP | None = None

    def __enter__(self):
        c = self.creds
        host = c.get("smtp_host") or c.get("host")
        port = int(c.get("smtp_port") or c.get("port") or 587)
        ctx = ssl.create_default_context()
        if port == 465:
            self._conn = smtplib.SMTP_SSL(host, port, timeout=60, context=ctx)
        else:
            self._conn = smtplib.SMTP(host, port, timeout=60)
            self._conn.starttls(context=ctx)
        self._conn.login(c["user"], c["password"])
        return self

    def send(self, msg: EmailMessage, to_addrs: list[str] | None = None) -> dict:
        """Returns refused recipients ({} = accepted). Raises on connection-level failure."""
        return self._conn.send_message(msg, to_addrs=to_addrs)

    def __exit__(self, *exc):
        if self._conn:
            try:
                self._conn.quit()
            except smtplib.SMTPException:
                pass
