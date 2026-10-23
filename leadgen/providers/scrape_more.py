"""More scrape adapters. Each fetch(url) returns (Page, units) in that provider's credit unit.

Jina Reader returns markdown (raw_html=False: no absence claims from it). The other four
are asked for the RAW HTML as the target served it, in their cheapest no-JS mode, so
scripts are intact and raw_html=True.

Error mapping is explicit per provider, from each one's current docs. A target-site
failure (provider says the target 404'd or was unreachable) is always ProviderError,
never quota. Per-second / concurrency throttles are ProviderError too: they clear in
seconds, and QuotaExhausted would park the provider for the whole period.
"""
from __future__ import annotations

import re

from .base import Page, ProviderError, QuotaExhausted, http_client

_HREF = re.compile(r'href=["\']([^"\'#]+)', re.I)
# Markdown links/images: [text](url "title") -> url. Stops at whitespace or ')'.
_MD_LINK = re.compile(r'\]\((https?://[^)\s]+)')


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def _units(resp, header: str, default: int) -> int:
    raw = resp.headers.get(header)
    try:
        return max(int(float(raw)), 0) if raw is not None else default
    except ValueError:
        return default


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


class JinaReader:
    """GET https://r.jina.ai/<url> with Accept: application/json. Keyless works (20 RPM)."""

    BASE = "https://r.jina.ai/"

    def __init__(self, key: str | None = None, **_):
        self.key = key

    def fetch(self, url: str):
        headers = {"Accept": "application/json", "X-With-Links-Summary": "true"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        with http_client(timeout=90) as c:
            resp = c.get(self.BASE + url, headers=headers)
        body = _json(resp)
        if resp.status_code >= 400 or not isinstance(body, dict):
            self._raise(resp, body)
        data = body.get("data") or {}
        target = data.get("httpStatus")
        if isinstance(target, (int, float)) and target >= 400:
            raise ProviderError(f"jina: target {url} returned HTTP {int(target)}")
        markdown = data.get("content") or ""
        links = _MD_LINK.findall(markdown)
        summary = data.get("links")
        if isinstance(summary, dict):          # {"anchor text": "url"}
            links += [v for v in summary.values() if isinstance(v, str)]
        elif isinstance(summary, list):
            links += [v if isinstance(v, str) else (v or {}).get("url", "") for v in summary]
        return Page(url=data.get("url") or url, html="", markdown=markdown,
                    links=_dedupe(links), raw_html=False), 1

    @staticmethod
    def _raise(resp, body):
        b = body if isinstance(body, dict) else {}
        status = b.get("status")
        msg = f"jina HTTP {resp.status_code} {status or ''}: {resp.text[:300]}"
        if resp.status_code == 402:
            # 40203 InsufficientBalanceError (token balance spent); 40202
            # TierFeatureConstraintError (plan does not allow it): both plan limits.
            raise QuotaExhausted(msg)
        if resp.status_code < 400:
            raise ProviderError(f"jina returned non-JSON: {resp.text[:300]}")
        # 429 RateLimitTriggeredError (RPM/TPM window), 409 BudgetExceededError
        # (our own X-Token-Budget), 422 DownstreamServiceFailure (target), 451, 5xx.
        raise ProviderError(msg)


class ZenRows:
    """GET https://api.zenrows.com/v1/?apikey=&url= : no js_render, no premium = 1 credit."""

    URL = "https://api.zenrows.com/v1/"
    QUOTA_CODES = {"AUTH004",   # usage exceeded for the billing period
                   "AUTH014",   # this key's credit cap reached
                   "AUTH010",   # feature not included in plan
                   "AUTH011",   # no subscription
                   "AUTH012"}   # subscription does not allow this product

    def __init__(self, key: str, **_):
        self.key = key

    def fetch(self, url: str):
        with http_client(timeout=180) as c:
            resp = c.get(self.URL, params={"apikey": self.key, "url": url})
        if resp.status_code >= 400:
            self._raise(resp)
        html = resp.text
        return Page(url=url, html=html, links=_HREF.findall(html), raw_html=True), \
            _units(resp, "X-Request-Credits", 1)

    def _raise(self, resp):
        body = _json(resp)
        code = (body or {}).get("code", "") if isinstance(body, dict) else ""
        msg = f"zenrows HTTP {resp.status_code} {code}: {resp.text[:300]}"
        if code in self.QUOTA_CODES or (resp.status_code == 402 and not code):
            raise QuotaExhausted(msg)
        # 429 AUTH006/AUTH008 concurrency/rate (seconds); 404 RESP002/RESP007 and
        # 422 RESP001 / 400 RESP008 are target failures; 400 REQS00x request problems.
        raise ProviderError(msg)


class ScrapingAnt:
    """GET https://api.scrapingant.com/v2/general?url=&browser=false : 1 credit, raw HTML."""

    URL = "https://api.scrapingant.com/v2/general"

    def __init__(self, key: str, **_):
        self.key = key

    def fetch(self, url: str):
        with http_client(timeout=90) as c:
            resp = c.get(self.URL, params={"url": url, "browser": "false"},
                         headers={"x-api-key": self.key})
        if resp.status_code >= 400:
            self._raise(resp)
        target = resp.headers.get("Ant-page-status-code")
        if target and target.isdigit() and int(target) >= 400:
            raise ProviderError(f"scrapingant: target {url} returned HTTP {target}")
        html = resp.text
        return Page(url=url, html=html, links=_HREF.findall(html), raw_html=True), \
            _units(resp, "Ant-credits-cost", 1)

    @staticmethod
    def _raise(resp):
        body = _json(resp)
        detail = str((body or {}).get("detail", "")) if isinstance(body, dict) else resp.text
        msg = f"scrapingant HTTP {resp.status_code}: {detail[:300]}"
        if resp.status_code == 403 and "quota limit reached" in detail.lower():
            raise QuotaExhausted(msg)
        # 403 token/IP, 409 free-plan concurrency (one active request), 404 unreachable
        # site, 423 target detection, 422 bad params, 5xx.
        raise ProviderError(msg)


class ScrapeDo:
    """GET https://api.scrape.do/?token=&url= : datacenter, no render = 1 credit, raw HTML."""

    URL = "https://api.scrape.do/"

    def __init__(self, key: str, **_):
        self.key = key

    def fetch(self, url: str):
        with http_client(timeout=120) as c:
            resp = c.get(self.URL, params={"token": self.key, "url": url})
        if resp.status_code >= 400:
            self._raise(resp)
        html = resp.text
        return Page(url=resp.headers.get("Scrape.do-Resolved-Url") or url, html=html,
                    links=_HREF.findall(html), raw_html=True), \
            _units(resp, "Scrape.do-Request-Cost", 1)

    @staticmethod
    def _raise(resp):
        text = resp.text[:300]
        msg = f"scrape.do HTTP {resp.status_code}: {text}"
        low = text.lower()
        if resp.status_code == 401 and not ("incorrect" in low or "throttled" in low):
            # Documented: "You have no credits or your subscription has been suspended".
            raise QuotaExhausted(msg)
        # 401 bad token / auth throttling, 429 concurrency, 404 target not found
        # (charged by scrape.do, still a target failure), 400, 502, 510.
        raise ProviderError(msg)


class ScraperApi:
    """GET https://api.scraperapi.com/?api_key=&url= : no render/premium = 1 credit, raw HTML."""

    URL = "https://api.scraperapi.com/"

    def __init__(self, key: str, **_):
        self.key = key

    def fetch(self, url: str):
        with http_client(timeout=75) as c:   # docs: allow 70 s for internal retries
            resp = c.get(self.URL, params={"api_key": self.key, "url": url})
        if resp.status_code >= 400:
            self._raise(resp)
        html = resp.text
        return Page(url=url, html=html, links=_HREF.findall(html), raw_html=True), \
            _units(resp, "sa-credit-cost", 1)

    @staticmethod
    def _raise(resp):
        msg = f"scraperapi HTTP {resp.status_code}: {resp.text[:300]}"
        if resp.status_code == 403:
            # "You have exhausted the API Credits available in this monthly cycle."
            # Not billing: overages are off unless enabled in the dashboard.
            raise QuotaExhausted(msg)
        # 404 target page missing (charged), 429 concurrency, 400 malformed, 401 key, 500.
        raise ProviderError(msg)


SCRAPE_MORE = {"jina_reader": JinaReader, "zenrows": ZenRows, "scrapingant": ScrapingAnt,
               "scrape_do": ScrapeDo, "scraperapi": ScraperApi}
