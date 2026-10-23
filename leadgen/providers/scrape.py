"""Scrape adapters: fetch one page of a prospect's own site. Each returns (Page, units)."""
from __future__ import annotations

import re

from .base import Page, ProviderError, check_response, http_client
from .firecrawl_usage import FirecrawlUsage

_HREF = re.compile(r'href=["\']([^"\'#]+)', re.I)


class FirecrawlScrape(FirecrawlUsage):
    def __init__(self, key: str, plan_credits: int = 1000, **_):
        self.key, self.plan_credits = key, plan_credits

    def fetch(self, url: str):
        with http_client(timeout=90) as c:
            resp = c.post("https://api.firecrawl.dev/v2/scrape",
                          headers={"Authorization": f"Bearer {self.key}"},
                          json={"url": url, "formats": ["markdown", "links", "rawHtml"],
                                "onlyMainContent": False})
        data = check_response(resp, "firecrawl").get("data", {})
        return Page(url=url, html=data.get("rawHtml") or data.get("html") or "",
                    markdown=data.get("markdown", ""), links=data.get("links", [])), 1


class NimbleExtract:
    def __init__(self, key: str, **_):
        self.key = key

    def fetch(self, url: str):
        with http_client(timeout=90) as c:
            resp = c.post("https://sdk.nimbleway.com/v2/extract",
                          headers={"Authorization": f"Bearer {self.key}"},
                          json={"url": url, "render": True})
        data = check_response(resp, "nimble")
        html = (data.get("data") or {}).get("html", "")
        return Page(url=url, html=html, links=_HREF.findall(html)), 1


class DirectFetch:
    """Free last resort. Plain GET of a public page, identified by our User-Agent."""

    def __init__(self, **_):
        pass

    def fetch(self, url: str):
        with http_client(timeout=30) as c:
            try:
                resp = c.get(url)
            except Exception as exc:  # DNS, TLS, timeouts: all "this provider failed"
                raise ProviderError(f"direct fetch {url}: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"direct fetch {url}: HTTP {resp.status_code}")
        html = resp.text
        return Page(url=str(resp.url), html=html, links=_HREF.findall(html)), 0


SCRAPE = {"firecrawl": FirecrawlScrape, "nimble": NimbleExtract, "direct": DirectFetch}
