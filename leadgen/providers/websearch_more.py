"""More web-search discovery adapters: Linkup and Brave Search.

Same contract as discovery.py: search(query, geo, limit) -> (candidates, units).
Both providers are usd_credit plans; units = 1 per request and the $ cost per
request is recorded in docs/providers/web_scrape.md.

Error mapping is explicit per provider (from each one's current docs) rather than
left to check_response's generic word lists, because both use 429 for two very
different things: "out of credits" (QuotaExhausted, park for the period) and
"too many requests right now" (ProviderError, transient, try the next provider).
"""
from __future__ import annotations

from .base import (BILLING_WORDS, BillingSignal, Candidate, ProviderError, QuotaExhausted,
                   http_client)


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


class LinkupSearch:
    """POST https://api.linkup.so/v1/search (docs.linkup.so). $0.005 per standard searchResults call."""

    kind = "web"
    URL = "https://api.linkup.so/v1/search"
    # 429 error.code values the official SDK maps to credit / budget exhaustion.
    QUOTA_CODES = {"INSUFFICIENT_FUNDS_CREDITS", "EXCEED_BUDGET_LIMIT"}

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int):
        with http_client() as c:
            resp = c.post(self.URL, headers={"Authorization": f"Bearer {self.key}"},
                          json={"q": f"{query} in {geo}", "depth": "standard",
                                "outputType": "searchResults", "maxResults": max(1, limit)})
        data = self._check(resp)
        if data is None:
            return [], 1
        out = [Candidate(name=r.get("name", ""), website=r["url"], source="linkup")
               for r in data.get("results", [])
               if r.get("url") and r.get("type", "text") == "text"]
        return out[:limit], 1

    def _check(self, resp):
        body = _json(resp)
        if resp.status_code < 400:
            if not isinstance(body, dict):
                raise ProviderError(f"linkup returned non-JSON: {resp.text[:300]}")
            return body
        err = (body or {}).get("error") if isinstance(body, dict) else None
        code = (err or {}).get("code", "") if isinstance(err, dict) else ""
        msg = f"linkup HTTP {resp.status_code} {code}: {resp.text[:300]}"
        if resp.status_code == 400 and code == "SEARCH_QUERY_NO_RESULT":
            return None  # documented "query yields no result": an empty page, not a failure
        if resp.status_code == 402:
            # x402 pay-per-request in USDC: the request would be paid for.
            raise BillingSignal(msg)
        if resp.status_code == 429:
            if code == "TOO_MANY_REQUESTS":
                raise ProviderError(msg)        # concurrency, transient
            raise QuotaExhausted(msg)           # credits / budget (or an unknown 429: be safe)
        raise ProviderError(msg)


class BraveSearch:
    """GET https://api.search.brave.com/res/v1/web/search. $5 per 1,000 requests ($0.005 each).

    Card on file: anything that smells of payment, billing or balance is a BillingSignal.
    """

    kind = "web"
    URL = "https://api.search.brave.com/res/v1/web/search"
    PAY_WORDS = BILLING_WORDS + ("payment", "balance", "prepaid", "credit card", "invoice",
                                 "spend", "usage limit")

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int):
        with http_client() as c:
            resp = c.get(self.URL, params={"q": f"{query} in {geo}", "count": min(max(limit, 1), 20)},
                         headers={"Accept": "application/json", "X-Subscription-Token": self.key})
        data = self._check(resp)
        results = ((data.get("web") or {}).get("results")) or []
        out = [Candidate(name=r.get("title", ""), website=r["url"], source="brave_search")
               for r in results if r.get("url")]
        return out[:limit], 1

    @staticmethod
    def _monthly_remaining(resp):
        """X-RateLimit-Remaining: '<per-second>, <per-month>'. None when absent/unparseable."""
        raw = resp.headers.get("X-RateLimit-Remaining", "")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        try:
            return int(parts[1]) if len(parts) > 1 else None
        except ValueError:
            return None

    def _check(self, resp):
        body = _json(resp)
        if resp.status_code < 400:
            if not isinstance(body, dict):
                raise ProviderError(f"brave returned non-JSON: {resp.text[:300]}")
            return body
        err = body.get("error") if isinstance(body, dict) else None
        err = err if isinstance(err, dict) else {}
        code = str(err.get("code", "")).upper()
        detail = str(err.get("detail", ""))
        low = (resp.text[:1000] + " " + code + " " + detail).lower()
        msg = f"brave HTTP {resp.status_code} {code}: {resp.text[:300]}"
        if resp.status_code == 402 or any(w in low for w in self.PAY_WORDS):
            raise BillingSignal(msg)
        if resp.status_code == 429:
            monthly = self._monthly_remaining(resp)
            if "QUOTA" in code or monthly == 0:
                raise QuotaExhausted(msg)
            if code == "RATE_LIMITED" and monthly and monthly > 0:
                raise ProviderError(msg)        # 1-second window only, transient
            raise QuotaExhausted(msg)           # ambiguous 429 on a card-on-file plan: stop
        raise ProviderError(msg)


WEB_MORE = {"linkup": LinkupSearch, "brave_search": BraveSearch}
