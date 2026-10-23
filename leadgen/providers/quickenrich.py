"""QuickEnrich: contacts with emails for a company domain.

Written from the official docs read 2026-10-23 (https://app.quickenrich.io/docs):
  GET https://app.quickenrich.io/api/employees/dataset-search?company_url=<domain>&has_email=true&page=1
  Auth: header `Authorization: Bearer <key>`.
  Cost: with no title filter, a flat 1 credit per page that has results; 0 when nothing matches
  (HTTP 200, data [], meta.reason EMPLOYEE_NOT_FOUND). meta.remaining_credits on every reply.
The API has no verification status (only email_verification_date), so every address it returns
is marked unverified and goes through the engine's own verifier before anything is sent.
Out-of-credits is not documented: 402/403, or a message about credits/plan/limit, is treated
as exhausted; 401 parks the key for the period; 429 (rate limit) is a plain retryable failure.
"""
from __future__ import annotations

import re

from .base import Person, ProviderError, QuotaExhausted, http_client

URL = "https://app.quickenrich.io/api/employees/dataset-search"
CREDIT_WORDS = re.compile(r"credit|quota|plan|limit reached|upgrade|subscription", re.I)


class QuickEnrich:
    def __init__(self, key: str, **_):
        self.key = key
        self.remaining_credits: int | None = None

    def find(self, domain: str):
        with http_client(timeout=60) as c:
            resp = c.get(URL, headers={"Authorization": f"Bearer {self.key}"},
                         # has_email must be 1/0: the API rejects "true" with a 422 (observed live).
                         params={"company_url": domain, "has_email": 1, "page": 1})
        try:
            data = resp.json()
        except ValueError:
            data = {}
        msg = str(data.get("message") or "") if isinstance(data, dict) else ""
        if resp.status_code == 401:
            raise QuotaExhausted(f"quickenrich: key refused (401) {msg[:100]}")
        if resp.status_code in (402, 403) or (resp.status_code >= 400 and resp.status_code != 429
                                              and CREDIT_WORDS.search(msg) and "rate limit" not in msg.lower()):
            raise QuotaExhausted(f"quickenrich HTTP {resp.status_code}: {msg[:150]}")
        if resp.status_code >= 400:
            raise ProviderError(f"quickenrich HTTP {resp.status_code}: {msg[:150]}")
        if not isinstance(data, dict) or data.get("success") is False:
            raise ProviderError(f"quickenrich: {msg[:150] or 'unexpected response'}")
        meta = data.get("meta") or {}
        if meta.get("remaining_credits") is not None:
            self.remaining_credits = int(meta["remaining_credits"])
        out = []
        for row in data.get("data") or []:
            email = str(row.get("email") or "").strip().lower()
            if not email or email in ("n/a", "null") or email.split("@")[-1] != domain:
                continue
            out.append(Person(email=email, first_name=row.get("first_name") or "",
                              last_name=row.get("last_name") or "", position=row.get("title") or "",
                              verified=None, source="quickenrich"))
        return out, int(meta.get("credits_used") or (1 if out else 0))
