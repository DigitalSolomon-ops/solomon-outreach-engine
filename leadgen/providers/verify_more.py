"""More email verification adapters. Each verify(email) returns (verdict, units).

Verdicts (base.VERDICTS): ok | catch_all | invalid | disposable | unknown.
Rule for every mapping here: only an explicit, mailbox-level "deliverable" from the
provider becomes "ok". Anything ambiguous is "unknown". Docs URLs and mapping tables
are in docs/providers/verify_people.md; the docs' own example responses are in
tests/fixtures/providers/.
"""
from __future__ import annotations

import httpx

from .base import ProviderError, QuotaExhausted, check_response, http_client

_CREDIT_WORDS = ("credit", "quota", "limit", "balance", "exceed")


def _quota_if(resp: httpx.Response, provider: str, codes: set[int]) -> None:
    """Raise QuotaExhausted for a provider's documented out-of-allowance status codes."""
    if resp.status_code in codes:
        raise QuotaExhausted(f"{provider} HTTP {resp.status_code}: {resp.text[:300]}")


def _rate_limited(resp: httpx.Response, provider: str, codes: set[int]) -> None:
    """A per-second/minute throttle is not an exhausted allowance: plain ProviderError."""
    if resp.status_code in codes:
        raise ProviderError(f"{provider} rate limited HTTP {resp.status_code}: {resp.text[:300]}")


class MailboxValidator:
    """https://www.mailboxvalidator.com/api-single-validation (API v2)."""

    URL = "https://api.mailboxvalidator.com/v2/validation/single"

    def __init__(self, key: str, **_):
        self.key = key

    def verify(self, email: str):
        with http_client(timeout=60) as c:
            resp = c.get(self.URL, params={"key": self.key, "email": email, "format": "json"})
        try:
            body = resp.json()
        except ValueError:
            body = None
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = err.get("error_code")
            msg = f"mailboxvalidator {code}: {err.get('error_message')}"
            if code == 10004:                       # Insufficient credits (HTTP 401)
                raise QuotaExhausted(msg)
            raise ProviderError(msg)
        data = check_response(resp, "mailboxvalidator")
        return self._map(data), 1

    @staticmethod
    def _map(d: dict) -> str:
        if d.get("is_syntax") is False or d.get("is_domain") is False:
            return "invalid"
        if d.get("is_disposable") is True:
            return "disposable"
        if d.get("is_suppressed") is True:          # on MailboxValidator's blacklist
            return "invalid"
        if d.get("is_catchall") is True:
            return "catch_all"
        if d.get("is_verified") is True and d.get("status") is True:
            return "ok"
        # Server reachable, not greylisted, and it said the mailbox does not exist.
        if (d.get("is_verified") is False and d.get("is_smtp") is True
                and d.get("is_server_down") is False and d.get("is_greylisted") is False):
            return "invalid"
        return "unknown"


class ZeroBounce:
    """https://www.zerobounce.net/docs/email-validation-api-quickstart/v2-validate-emails"""

    URL = "https://api.zerobounce.net/v2/validate"
    STATUS = {"valid": "ok", "catch-all": "catch_all", "invalid": "invalid", "unknown": "unknown",
              "spamtrap": "invalid", "abuse": "invalid"}

    def __init__(self, key: str, **_):
        self.key = key

    def verify(self, email: str):
        with http_client(timeout=40) as c:
            resp = c.get(self.URL, params={"api_key": self.key, "email": email, "timeout": 30})
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            err = body["error"]
            # Docs: {"error":"Invalid API Key or your account ran out of credits"} - one
            # message for both cases, so it disables the provider for the period.
            if "credit" in err.lower():
                raise QuotaExhausted(f"zerobounce: {err}")
            raise ProviderError(f"zerobounce: {err}")
        data = check_response(resp, "zerobounce")
        verdict = self._map(data)
        # "will never consume a credit for any unknown result"
        return verdict, (0 if (data.get("status") or "").lower() == "unknown" else 1)

    @classmethod
    def _map(cls, d: dict) -> str:
        status = (d.get("status") or "").lower()
        sub = (d.get("sub_status") or "").lower()
        if status == "do_not_mail":
            if sub == "disposable":
                return "disposable"
            if sub == "role_based_catch_all":
                return "catch_all"
            if sub == "role_based":
                # A role address with no deliverability claim: not "ok", not proven bad.
                return "unknown"
            return "invalid"                        # global_suppression, possible_trap, toxic, ...
        return cls.STATUS.get(status, "unknown")


class AbstractEmail:
    """https://docs.abstractapi.com/api/email-validation"""

    URL = "https://emailvalidation.abstractapi.com/v1/"

    def __init__(self, key: str, **_):
        self.key = key

    def verify(self, email: str):
        with http_client(timeout=30) as c:
            resp = c.get(self.URL, params={"api_key": self.key, "email": email, "auto_correct": "false"})
        _quota_if(resp, "abstract_email", {422})          # "Quota reached" (free plans)
        _rate_limited(resp, "abstract_email", {429})      # 1 request/second on free plans
        data = check_response(resp, "abstract_email")
        return self._map(data), 1                          # every request counts as a credit

    @staticmethod
    def _flag(d: dict, name: str):
        v = d.get(name)
        return v.get("value") if isinstance(v, dict) else v

    @classmethod
    def _map(cls, d: dict) -> str:
        deliv = (d.get("deliverability") or "").upper()
        if cls._flag(d, "is_valid_format") is False:
            return "invalid"
        if cls._flag(d, "is_disposable_email") is True:
            return "disposable"
        if deliv == "UNDELIVERABLE":
            return "invalid"
        if cls._flag(d, "is_catchall_email") is True:
            return "catch_all"
        # Abstract is weaker: DELIVERABLE counts only when its SMTP check passed.
        if deliv == "DELIVERABLE" and cls._flag(d, "is_smtp_valid") is True:
            return "ok"
        return "unknown"


class Reoon:
    """https://www.reoon.com/articles/api-documentation-of-reoon-email-verifier/

    Uses POWER mode: the only mode that checks the individual inbox (quick mode marks
    any address on a good domain valid). Free daily credits may need a human login to
    renew, so a credit/limit refusal is QuotaExhausted and never assumed to renew.
    """

    URL = "https://emailverifier.reoon.com/api/v1/verify"
    POWER = {"safe": "ok", "invalid": "invalid", "disabled": "invalid", "disposable": "disposable",
             "catch_all": "catch_all", "spamtrap": "invalid", "unknown": "unknown",
             "inbox_full": "unknown"}               # temporary state: not proven bad, not deliverable

    def __init__(self, key: str, mode: str = "power", **_):
        self.key = key
        self.mode = mode

    def verify(self, email: str):
        with http_client(timeout=120) as c:         # power mode can take over a minute
            resp = c.get(self.URL, params={"email": email, "key": self.key, "mode": self.mode})
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("status") == "error":
            reason = str(body.get("reason") or "")
            if any(w in reason.lower() for w in _CREDIT_WORDS):
                raise QuotaExhausted(f"reoon: {reason}")
            raise ProviderError(f"reoon: {reason}")
        if resp.status_code in (401, 403) and any(w in resp.text.lower() for w in _CREDIT_WORDS):
            raise QuotaExhausted(f"reoon HTTP {resp.status_code}: {resp.text[:300]}")
        data = check_response(resp, "reoon")
        return self._map(data), 1

    @classmethod
    def _map(cls, d: dict) -> str:
        status = (d.get("status") or "").lower()
        if (d.get("verification_mode") or "").lower() != "power":
            # Quick-mode answer: no inbox check, so "valid" proves nothing about the mailbox.
            return {"invalid": "invalid", "disposable": "disposable",
                    "spamtrap": "invalid"}.get(status, "unknown")
        if status == "role_account":
            if d.get("is_catch_all") is True:
                return "catch_all"
            if d.get("is_deliverable") is True and d.get("is_safe_to_send") is True:
                return "ok"
            return "unknown"
        return cls.POWER.get(status, "unknown")


class HunterVerify:
    """https://hunter.io/api-documentation/v2#email-verifier

    Costs 0.5 Hunter credit; reported as units=1 because units are integers. The pool
    is shared with hunter_domain.
    """

    URL = "https://api.hunter.io/v2/email-verifier"
    STATUS = {"valid": "ok", "invalid": "invalid", "accept_all": "catch_all",
              "disposable": "disposable", "unknown": "unknown",
              "webmail": "unknown"}                 # free-mail provider, no mailbox verdict

    def __init__(self, key: str, **_):
        self.key = key

    def verify(self, email: str):
        with http_client(timeout=40) as c:
            resp = c.get(self.URL, params={"email": email, "api_key": self.key})
        if resp.status_code in (202, 222):
            # 202: still running (counted once); 222: remote SMTP failure. No verdict.
            return "unknown", 1
        if resp.status_code == 451:
            return "invalid", 0                     # claimed_email: owner asked not to be processed
        _quota_if(resp, "hunter_verify", {429})     # "You have reached your usage limit"
        _rate_limited(resp, "hunter_verify", {403})  # Hunter uses 403 for the rate limit
        data = check_response(resp, "hunter_verify").get("data") or {}
        return self.STATUS.get((data.get("status") or "").lower(), "unknown"), 1


class GetProspectVerify:
    """https://getprospect.com/api-docs/email-verifier (API v2)."""

    URL = "https://api.getprospect.com/v2/email/verify"
    STATUS = {"valid": "ok", "invalid": "invalid", "accept_all": "catch_all", "unknown": "unknown"}

    def __init__(self, key: str, **_):
        self.key = key

    def verify(self, email: str):
        with http_client(timeout=90) as c:          # docs: runs up to 75 s, use >= 80 s
            resp = c.post(self.URL, json={"data": {"email": email}},
                          headers={"x-api-key": self.key})
        _quota_if(resp, "getprospect_verify", {402})        # PAYMENT_REQUIRED = credits at zero
        _rate_limited(resp, "getprospect_verify", {408, 429})  # timeout / Retry-After
        data = check_response(resp, "getprospect_verify")
        body = data.get("data") or {}
        if data.get("success") is False:
            return "unknown", 0                     # VERIFICATION_ERROR: not charged
        status = (body.get("status") or "").lower()
        units = 1 if status in ("valid", "invalid") else 0   # charged only for valid/invalid
        if (body.get("domain_status") or "").lower() == "disposable":
            return "disposable", units
        return self.STATUS.get(status, "unknown"), units


VERIFY_MORE = {
    "mailboxvalidator": MailboxValidator,
    "zerobounce": ZeroBounce,
    "abstract_email": AbstractEmail,
    "reoon": Reoon,
    "hunter_verify": HunterVerify,
    "getprospect_verify": GetProspectVerify,
}
