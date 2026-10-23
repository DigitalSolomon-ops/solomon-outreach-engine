"""Anymail Finder: company-domain email search and email verification.

Written from the official docs read 2026-10-23 (https://anymailfinder.com/email-finder-api/docs):
  POST https://api.anymailfinder.com/v5.1/find-email/company   {"domain": ...}
  POST https://api.anymailfinder.com/v5.1/verify-email          {"email": ...}
  GET  https://api.anymailfinder.com/v5.1/account               (free; credits_left)
Auth: header `Authorization: <key>` (no Bearer prefix).
Credits: a company search costs 1 credit only when valid emails are found; a verification
costs 0.2 credit whatever the result. Out of credits is HTTP 402 {"error": "upgrade_needed"}.
Since 2026-10-05 company searches never return risky emails (they come back not_found).
"""
from __future__ import annotations

from .base import Person, ProviderError, QuotaExhausted, http_client

BASE = "https://api.anymailfinder.com/v5.1"


def _check(resp, what: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    err = (data.get("error") or "") if isinstance(data, dict) else ""
    if resp.status_code == 402 or err == "upgrade_needed":
        raise QuotaExhausted(f"anymailfinder {what}: out of credits")
    if resp.status_code == 401 or err == "unauthorized":
        raise QuotaExhausted(f"anymailfinder {what}: key refused (401)")  # park it for the period
    if resp.status_code >= 400:
        raise ProviderError(f"anymailfinder {what} HTTP {resp.status_code}: {resp.text[:200]}")
    if not isinstance(data, dict):
        raise ProviderError(f"anymailfinder {what}: unexpected response")
    return data


class AnymailFinder:
    """People stage: business emails Anymail Finder has verified as valid for a domain."""

    def __init__(self, key: str, **_):
        self.key = key

    def find(self, domain: str):
        with http_client(timeout=180) as c:  # docs: searches are real-time, allow 180 s
            resp = c.post(f"{BASE}/find-email/company", headers={"Authorization": self.key},
                          json={"domain": domain, "email_type": "any"})
        data = _check(resp, "company search")
        units = float(data.get("credits_charged") or 0)
        if data.get("email_status") != "valid":
            return [], units
        out = []
        for email in data.get("valid_emails") or []:
            e = str(email).strip().lower()
            if e.split("@")[-1] == domain:
                out.append(Person(email=e, verified=True, source="anymailfinder"))
        return out, units


class AnymailFinderVerify:
    """Verify stage: valid -> ok; risky is inconclusive -> unknown; invalid -> invalid."""

    MAP = {"valid": "ok", "risky": "unknown", "invalid": "invalid"}

    def __init__(self, key: str, **_):
        self.key = key

    def verify(self, email: str):
        with http_client(timeout=60) as c:
            resp = c.post(f"{BASE}/verify-email", headers={"Authorization": self.key}, json={"email": email})
        data = _check(resp, "verify")
        return self.MAP.get(str(data.get("email_status") or "").lower(), "unknown"), \
            float(data.get("credits_charged") or 0.2)

    def credits_left(self) -> float | None:
        with http_client(timeout=30) as c:
            resp = c.get(f"{BASE}/account", headers={"Authorization": self.key})
        return _check(resp, "account").get("credits_left")
