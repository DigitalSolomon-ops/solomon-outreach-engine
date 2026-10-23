"""Shared provider plumbing: result types, errors, and quota detection.

Adapter contract (one class per provider, registered in that stage's dict):

  discovery  search(query, geo, limit)  -> (list[Candidate], units)
  scrape     fetch(url)                 -> (Page, units)
  verify     verify(email)              -> (verdict, units)
  people     find(domain)               -> (list[Person], units)
  sizing     establishments(naics, state_fips, county_fips) -> (int | None, units)

Optional on any adapter: usage() -> int | None, the provider's OWN count of
units used this period (card-on-file providers read it before each run).

Errors: QuotaExhausted for "out of free allowance", BillingSignal for anything
that says money would be charged (payment required, overage, billing), and
ProviderError for everything else.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# Status codes providers use to say "you are out of credits / rate limited".
# 432/433 are Tavily's plan and pay-as-you-go limit codes.
QUOTA_STATUS = {402, 429, 432, 433}
QUOTA_WORDS = ("insufficient credits", "out of credits", "run out of searches",
               "quota", "credit limit", "usage limit", "plan limit", "exceeded your",
               "not enough credits", "no credits", "credits exhausted", "limit reached",
               "insufficient balance")
BILLING_WORDS = ("payment required", "billing", "overage", "upgrade your plan to continue",
                 "add a payment method", "charged")

# Verdicts every verify adapter maps onto.
VERDICTS = ("ok", "catch_all", "invalid", "disposable", "unknown", "mx_only", "no_mx")


class ProviderError(Exception):
    """A call failed for a reason other than quota. The next provider is tried; budget untouched."""


class QuotaExhausted(ProviderError):
    """The provider is out of free allowance for now. It is marked exhausted for the period."""


class BillingSignal(QuotaExhausted):
    """The provider said it would bill (or did). Disabled for the period and Marcus is emailed."""


@dataclass
class Candidate:
    name: str
    website: str
    phone: str = ""
    address: str = ""
    category: str = ""
    source: str = ""
    place_id: str = ""            # Google Places: the ONLY field we may keep
    rating: float | None = None
    review_count: int | None = None
    categories: list[str] = field(default_factory=list)
    hours: list[str] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list, repr=False)   # transient, never stored raw
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def domain(self) -> str:
        return registrable_domain(self.website)


@dataclass
class Page:
    url: str
    html: str = ""
    markdown: str = ""
    links: list[str] = field(default_factory=list)
    # True only when html is the page as a browser got it (scripts intact).
    # Text/markdown readers (Jina) set False so no absence claim is made from them.
    raw_html: bool = True


@dataclass
class Person:
    email: str
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    confidence: int | None = None      # provider's 0-100 score when it gives one
    verified: bool | None = None       # provider says it verified the mailbox
    source: str = ""


def registrable_domain(url: str) -> str:
    """'https://www.Foo.com/a' -> 'foo.com'. Good enough for dedupe; not a PSL lookup."""
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def check_response(resp: httpx.Response, provider: str) -> dict:
    """Raise BillingSignal / QuotaExhausted / ProviderError on failure, return parsed JSON on success."""
    text = resp.text[:500]
    low = text.lower()
    if resp.status_code == 402 and any(w in low for w in BILLING_WORDS):
        raise BillingSignal(f"{provider} HTTP 402: {text}")
    if resp.status_code in QUOTA_STATUS:
        raise QuotaExhausted(f"{provider} HTTP {resp.status_code}: {text}")
    if resp.status_code >= 400:
        if any(w in low for w in BILLING_WORDS):
            raise BillingSignal(f"{provider} HTTP {resp.status_code}: {text}")
        if any(w in low for w in QUOTA_WORDS):
            raise QuotaExhausted(f"{provider} HTTP {resp.status_code}: {text}")
        raise ProviderError(f"{provider} HTTP {resp.status_code}: {text}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError(f"{provider} returned non-JSON: {text}") from exc
    # Some APIs return 200 with an error body (SerpApi does).
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        err = data["error"]
        if any(w in err.lower() for w in BILLING_WORDS):
            raise BillingSignal(f"{provider}: {err}")
        if any(w in err.lower() for w in QUOTA_WORDS):
            raise QuotaExhausted(f"{provider}: {err}")
        if "hasn't returned any results" in err.lower():
            return {}
        raise ProviderError(f"{provider}: {err}")
    return data


def http_client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": "SolomonLeadgen/1.0 (+https://digitalsolomon.com)"})
