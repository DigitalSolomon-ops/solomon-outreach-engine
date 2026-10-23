"""People-data adapters. Each find(domain) returns (list[Person], units).

Only addresses ON the queried domain are returned, only as the provider gave them
(no pattern guessing here, and provider-"inferred" addresses are dropped where the
provider labels them). Person.verified is True only when the provider explicitly says
the address is verified/valid. Docs URLs are in docs/providers/verify_people.md.
"""
from __future__ import annotations

import json
import math

import httpx

from .base import Person, ProviderError, QuotaExhausted, check_response, http_client, registrable_domain


def _on_domain(email: str, domain: str) -> bool:
    return bool(email) and email.lower().rsplit("@", 1)[-1] == domain


def _norm(domain: str) -> str:
    return registrable_domain(domain)


class GetProspect:
    """POST https://api.getprospect.com/v2/people/search with enrich_email.

    Searching is free; each revealed email costs 1 email credit. The result set is
    capped with max_person_per_company and restricted to contacts whose stored email
    status is "valid", so spend per domain is at most `max_people` credits.
    """

    URL = "https://api.getprospect.com/v2/people/search"

    def __init__(self, key: str, max_people: int = 3, **_):
        self.key = key
        self.max_people = max(1, min(int(max_people), 25))

    def find(self, domain: str):
        domain = _norm(domain)
        body = {
            "filters": {
                "company": {"domain": {"include": [domain], "match_mode": "PRIMARY"}},
                "contact_details": {"email": {"status": ["valid"]}},
                "max_person_per_company": self.max_people,
            },
            "page": 1,
            "enrich_email": True,
        }
        with http_client(timeout=60) as c:
            resp = c.post(self.URL, json=body, headers={"x-api-key": self.key})
        if resp.status_code == 402:                 # PAYMENT_REQUIRED: credit balance at zero
            raise QuotaExhausted(f"getprospect HTTP 402: {resp.text[:300]}")
        if resp.status_code == 429:                 # search rate limit, Retry-After
            raise ProviderError(f"getprospect rate limited: {resp.text[:300]}")
        data = check_response(resp, "getprospect")
        if data.get("success") is False:
            raise ProviderError(f"getprospect: {data.get('errors')}")
        people, revealed = [], 0
        for row in data.get("data") or []:
            em = row.get("email") or {}
            addr = (em.get("address") or "").strip()
            if not addr:
                continue
            revealed += 1                           # an address served = a reveal (charged unless already owned)
            if not _on_domain(addr, domain):
                continue
            pos = row.get("primary_position") or {}
            people.append(Person(
                email=addr.lower(), first_name=row.get("first_name") or "",
                last_name=row.get("last_name") or "", position=pos.get("job_title") or "",
                verified=(em.get("status") == "valid"), source="getprospect"))
        return people, revealed


class HunterDomain:
    """GET https://api.hunter.io/v2/domain-search (free plan: at most 10 per domain).

    provenance=found asks only for addresses seen on a public page; any address Hunter
    labels "inferred" (worked out from the domain's pattern) is dropped anyway.
    A query is counted only when it returns at least one result.
    """

    URL = "https://api.hunter.io/v2/domain-search"

    def __init__(self, key: str, limit: int = 10, **_):
        self.key = key
        self.limit = max(1, min(int(limit), 10))

    def find(self, domain: str):
        domain = _norm(domain)
        with http_client(timeout=40) as c:
            resp = c.get(self.URL, params={"domain": domain, "api_key": self.key,
                                           "limit": self.limit, "provenance": "found"})
        if resp.status_code == 429:                 # "You have reached your usage limit"
            raise QuotaExhausted(f"hunter_domain HTTP 429: {resp.text[:300]}")
        if resp.status_code == 403:                 # Hunter's rate-limit code
            raise ProviderError(f"hunter_domain rate limited: {resp.text[:300]}")
        data = check_response(resp, "hunter_domain").get("data") or {}
        emails = data.get("emails") or []
        people = []
        for e in emails:
            addr = (e.get("value") or "").strip()
            if not _on_domain(addr, domain) or (e.get("provenance") or "found") == "inferred":
                continue
            ver = (e.get("verification") or {}).get("status")
            people.append(Person(
                email=addr.lower(), first_name=e.get("first_name") or "",
                last_name=e.get("last_name") or "", position=e.get("position") or "",
                confidence=e.get("confidence"), verified=(ver == "valid"), source="hunter"))
        return people, (1 if emails else 0)


class Tomba:
    """GET https://api.tomba.io/v1/domain-search. Auth is TWO values (X-Tomba-Key +
    X-Tomba-Secret); base's single `key` is worked around: pass key="KEY:SECRET",
    key='{"key":..,"secret":..}', or key=KEY with secret=SECRET.

    Charged 1 search credit per 10 requested (limit=10 -> 1) only when an address is
    returned. Addresses Tomba last checked as "invalid" are dropped.
    """

    URL = "https://api.tomba.io/v1/domain-search"

    def __init__(self, key, secret: str | None = None, **_):
        k, s = key, secret
        if isinstance(key, dict):
            k, s = key.get("key"), key.get("secret")
        elif isinstance(key, str) and not s:
            txt = key.strip()
            if txt.startswith("{"):
                obj = json.loads(txt)
                k, s = obj.get("key"), obj.get("secret")
            elif ":" in txt:
                k, s = txt.split(":", 1)
        if not k or not s:
            raise ProviderError("tomba needs both an API key and a secret")
        self.key, self.secret = k.strip(), s.strip()

    def find(self, domain: str):
        domain = _norm(domain)
        with http_client(timeout=40) as c:
            resp = c.get(self.URL, params={"domain": domain, "limit": 10},
                         headers={"X-Tomba-Key": self.key, "X-Tomba-Secret": self.secret})
        if resp.status_code in (402, 429):          # quota_exceeded / daily rate_limit
            raise QuotaExhausted(f"tomba HTTP {resp.status_code}: {resp.text[:300]}")
        data = check_response(resp, "tomba").get("data") or {}
        emails = data.get("emails") or []
        people = []
        for e in emails:
            addr = (e.get("email") or "").strip()
            ver = (e.get("verification") or {}).get("status")
            if not _on_domain(addr, domain) or ver == "invalid":
                continue
            people.append(Person(
                email=addr.lower(), first_name=e.get("first_name") or "",
                last_name=e.get("last_name") or "", position=e.get("position") or "",
                confidence=e.get("score"), verified=(ver == "valid"), source="tomba"))
        return people, (1 if emails else 0)


class Apollo:
    """Two documented calls:
      1. POST /api/v1/mixed_people/api_search (0 credits) with q_organization_domains_list[]
         and contact_email_status[]=verified -> person ids (no emails in this response).
      2. POST /api/v1/people/bulk_match with those ids -> emails (1 credit per person with data).
    Only emails Apollo marks email_status "verified" and that sit on the domain are kept.
    A 403 (API_INACCESSIBLE: plan lacks API access / endpoint) is QuotaExhausted
    "plan refused" so rotation disables Apollo for the period.
    """

    BASE = "https://api.apollo.io/api/v1"
    PLAN_WORDS = ("not available on your plan", "api_inaccessible", "not authorized to access",
                  "endpoint_access_denied", "upgrade your plan to access")

    def __init__(self, key: str, max_people: int = 3, **_):
        self.key = key
        self.max_people = max(1, min(int(max_people), 10))   # bulk_match takes up to 10

    def _check(self, resp: httpx.Response, what: str) -> dict:
        low = resp.text[:1000].lower()
        if resp.status_code == 403 or (resp.status_code >= 400 and any(w in low for w in self.PLAN_WORDS)):
            raise QuotaExhausted(f"plan refused: apollo {what} HTTP {resp.status_code}: {resp.text[:300]}")
        return check_response(resp, f"apollo {what}")

    def find(self, domain: str):
        domain = _norm(domain)
        headers = {"x-api-key": self.key, "Content-Type": "application/json",
                   "Cache-Control": "no-cache", "accept": "application/json"}
        with http_client(timeout=60) as c:
            resp = c.post(f"{self.BASE}/mixed_people/api_search", headers=headers, params={
                "q_organization_domains_list[]": domain,
                "contact_email_status[]": "verified",
                "page": 1, "per_page": self.max_people})
            ids = [p["id"] for p in (self._check(resp, "search").get("people") or []) if p.get("id")]
            if not ids:
                return [], 0
            resp = c.post(f"{self.BASE}/people/bulk_match", headers=headers,
                          params={"reveal_personal_emails": "false", "reveal_phone_number": "false"},
                          json={"details": [{"id": i} for i in ids[: self.max_people]]})
            data = self._check(resp, "bulk_match")
        if data.get("status") == "failed":
            msg = f"apollo bulk_match {data.get('error_code')}: {data.get('error_message')}"
            if "credit" in msg.lower():
                raise QuotaExhausted(msg)
            raise ProviderError(msg)
        people = []
        for m in data.get("matches") or []:
            if not m:
                continue
            addr = (m.get("email") or "").strip()
            if not _on_domain(addr, domain) or m.get("email_status") != "verified":
                continue
            people.append(Person(
                email=addr.lower(), first_name=m.get("first_name") or "",
                last_name=m.get("last_name") or "", position=m.get("title") or "",
                verified=True, source="apollo"))
        credits = data.get("credits_consumed")
        units = math.ceil(float(credits)) if credits is not None else len([m for m in data.get("matches") or [] if m])
        return people, units


PEOPLE = {"getprospect": GetProspect, "hunter_domain": HunterDomain, "tomba": Tomba, "apollo": Apollo}
