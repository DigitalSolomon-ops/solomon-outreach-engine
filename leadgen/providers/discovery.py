"""Discovery adapters: turn 'segment in geo' into candidate businesses.

Each search() returns (candidates, units_used). Units are in the provider's own
billing unit so the ledger can compare against the configured free cap.
"""
from __future__ import annotations

from .base import Candidate, ProviderError, check_response, http_client
from .firecrawl_usage import FirecrawlUsage


class SerpApiMaps:
    kind = "maps"

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int):
        with http_client() as c:
            resp = c.get("https://serpapi.com/search.json", params={
                "engine": "google_maps", "type": "search",
                "q": f"{query} {geo}", "api_key": self.key})
        data = check_response(resp, "serpapi")
        out = []
        for r in (data.get("local_results") or [])[:limit]:
            if not r.get("website"):
                continue  # no site = nothing to scrape or personalize from
            out.append(Candidate(name=r.get("title", ""), website=r["website"],
                                 phone=r.get("phone", ""), address=r.get("address", ""),
                                 category=r.get("type", ""), source="serpapi_maps"))
        return out, 1


class ApifyMaps:
    kind = "maps"

    def __init__(self, key: str, actor: str = "compass~crawler-google-places", **_):
        self.key, self.actor = key, actor

    def search(self, query: str, geo: str, limit: int):
        url = f"https://api.apify.com/v2/acts/{self.actor}/run-sync-get-dataset-items"
        with http_client(timeout=300) as c:
            resp = c.post(url, params={"token": self.key}, json={
                "searchStringsArray": [query], "locationQuery": geo,
                "maxCrawledPlacesPerSearch": limit, "language": "en"})
        data = check_response(resp, "apify")
        if not isinstance(data, list):
            raise ProviderError(f"apify: expected a list of places, got {type(data).__name__}")
        out = []
        for r in data:
            if r.get("permanentlyClosed") or not r.get("website"):
                continue
            out.append(Candidate(name=r.get("title", ""), website=r["website"],
                                 phone=r.get("phone", ""), address=r.get("address", ""),
                                 category=r.get("categoryName", ""), source="apify_maps"))
        return out, len(data)


class FirecrawlSearch(FirecrawlUsage):
    kind = "web"

    def __init__(self, key: str, plan_credits: int = 1000, **_):
        self.key, self.plan_credits = key, plan_credits

    def search(self, query: str, geo: str, limit: int):
        with http_client() as c:
            resp = c.post("https://api.firecrawl.dev/v2/search",
                          headers={"Authorization": f"Bearer {self.key}"},
                          json={"query": f"{query} in {geo}", "limit": min(limit, 20)})
        data = check_response(resp, "firecrawl")
        body = data.get("data", data)
        results = body.get("web", []) if isinstance(body, dict) else body  # v2 dict, v1 list
        out = [Candidate(name=r.get("title", ""), website=r.get("url", ""), source="firecrawl")
               for r in results if r.get("url")]
        return out, int(data.get("creditsUsed") or 2)


class NimbleSearch:
    kind = "web"

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int):
        with http_client() as c:
            resp = c.post("https://sdk.nimbleway.com/v2/search",
                          headers={"Authorization": f"Bearer {self.key}"},
                          json={"query": f"{query} in {geo}", "max_results": limit,
                                "search_depth": "lite"})
        data = check_response(resp, "nimble")
        out = [Candidate(name=r.get("title", ""), website=r.get("url", ""), source="nimble")
               for r in data.get("results", []) if r.get("url")]
        return out, 1


class TavilySearch:
    kind = "web"

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int):
        with http_client() as c:
            resp = c.post("https://api.tavily.com/search",
                          headers={"Authorization": f"Bearer {self.key}"},
                          json={"query": f"{query} in {geo}", "max_results": limit})
        data = check_response(resp, "tavily")
        out = [Candidate(name=r.get("title", ""), website=r.get("url", ""), source="tavily")
               for r in data.get("results", []) if r.get("url")]
        return out, 1


class ExaSearch:
    kind = "web"

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int):
        with http_client() as c:
            resp = c.post("https://api.exa.ai/search", headers={"x-api-key": self.key},
                          json={"query": f"{query} in {geo}", "numResults": limit, "type": "auto"})
        data = check_response(resp, "exa")
        out = [Candidate(name=r.get("title", ""), website=r.get("url", ""), source="exa")
               for r in data.get("results", []) if r.get("url")]
        return out, 1


DISCOVERY = {
    "serpapi_maps": SerpApiMaps,
    "apify_maps": ApifyMaps,
    "firecrawl": FirecrawlSearch,
    "nimble": NimbleSearch,
    "tavily": TavilySearch,
    "exa": ExaSearch,
}
