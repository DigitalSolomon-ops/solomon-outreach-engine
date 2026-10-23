"""Web-search (Linkup, Brave) and scrape (Jina, ZenRows, ScrapingAnt, Scrape.do, ScraperAPI)
adapters, driven by each provider's documented responses through httpx.MockTransport.
No network, no keys."""
import json
from pathlib import Path

import httpx
import pytest

from leadgen.providers import BillingSignal, ProviderError, QuotaExhausted
from leadgen.providers import scrape_more, websearch_more
from leadgen.providers.scrape_more import SCRAPE_MORE
from leadgen.providers.websearch_more import WEB_MORE

FIX = Path(__file__).parent / "fixtures" / "providers"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def to_response(spec):
    kw = {"headers": spec.get("headers", {})}
    if "json" in spec:
        kw["json"] = spec["json"]
    else:
        kw["text"] = spec.get("body", "")
    return httpx.Response(spec["status"], **kw)


@pytest.fixture
def serve(monkeypatch):
    """serve(module, spec) -> list of captured requests; every call returns spec."""
    def _serve(module, spec):
        seen = []

        def handler(request):
            seen.append(request)
            return to_response(spec)

        monkeypatch.setattr(module, "http_client",
                            lambda timeout=60.0: httpx.Client(transport=httpx.MockTransport(handler)))
        return seen
    return _serve


# ------------------------------------------------------------------ web search
def test_linkup_parses_search_results(serve):
    seen = serve(websearch_more, fixture("linkup")["ok"])
    cands, units = WEB_MORE["linkup"]("k").search("HVAC", "Gastonia, NC", 5)
    assert units == 1
    assert [c.website for c in cands] == ["https://www.microsoft.com/investor/reports/ar24/index.html"]
    assert cands[0].name == "Microsoft 2024 Annual Report" and cands[0].source == "linkup"
    req = seen[0]
    assert req.url == "https://api.linkup.so/v1/search"
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body == {"q": "HVAC in Gastonia, NC", "depth": "standard",
                    "outputType": "searchResults", "maxResults": 5}


def test_linkup_out_of_credits_is_quota(serve):
    serve(websearch_more, fixture("linkup")["quota"])
    with pytest.raises(QuotaExhausted) as e:
        WEB_MORE["linkup"]("k").search("q", "g", 3)
    assert not isinstance(e.value, BillingSignal)


def test_linkup_concurrency_429_is_transient_and_no_result_is_empty(serve):
    serve(websearch_more, fixture("linkup")["rate_limited"])
    with pytest.raises(ProviderError) as e:
        WEB_MORE["linkup"]("k").search("q", "g", 3)
    assert not isinstance(e.value, QuotaExhausted)
    serve(websearch_more, fixture("linkup")["no_result"])
    assert WEB_MORE["linkup"]("k").search("q", "g", 3) == ([], 1)


def test_linkup_402_payment_is_billing(serve):
    serve(websearch_more, fixture("linkup")["payment"])
    with pytest.raises(BillingSignal):
        WEB_MORE["linkup"]("k").search("q", "g", 3)


def test_brave_parses_web_results(serve):
    seen = serve(websearch_more, fixture("brave_search")["ok"])
    cands, units = WEB_MORE["brave_search"]("k").search("HVAC", "Gastonia, NC", 50)
    assert units == 1
    assert [(c.name, c.website) for c in cands] == [
        ("Python Web Frameworks", "https://example.com/python-frameworks")]
    req = seen[0]
    assert req.url.host == "api.search.brave.com" and req.url.path == "/res/v1/web/search"
    assert req.url.params["q"] == "HVAC in Gastonia, NC"
    assert req.url.params["count"] == "20"          # documented max
    assert req.headers["x-subscription-token"] == "k"


def test_brave_quota_is_quota_not_billing(serve):
    serve(websearch_more, fixture("brave_search")["quota"])
    with pytest.raises(QuotaExhausted) as e:
        WEB_MORE["brave_search"]("k").search("q", "g", 3)
    assert not isinstance(e.value, BillingSignal)


def test_brave_billing_is_billing_signal(serve):
    serve(websearch_more, fixture("brave_search")["billing"])
    with pytest.raises(BillingSignal):
        WEB_MORE["brave_search"]("k").search("q", "g", 3)


def test_brave_per_second_rate_limit_is_transient(serve):
    serve(websearch_more, fixture("brave_search")["rate_limited"])
    with pytest.raises(ProviderError) as e:
        WEB_MORE["brave_search"]("k").search("q", "g", 3)
    assert not isinstance(e.value, QuotaExhausted)


def test_brave_payment_words_in_any_error_are_billing(serve):
    serve(websearch_more, {"status": 422, "json": {"type": "ErrorResponse", "error": {
        "id": "x", "status": 422, "code": "SUBSCRIPTION_INACTIVE",
        "detail": "Payment method declined"}}})
    with pytest.raises(BillingSignal):
        WEB_MORE["brave_search"]("k").search("q", "g", 3)


# ---------------------------------------------------------------------- scrape
def test_jina_parses_markdown_keyless(serve):
    seen = serve(scrape_more, fixture("jina_reader")["ok"])
    page, units = SCRAPE_MORE["jina_reader"]().fetch("https://example.com/")
    assert units == 1
    assert page.raw_html is False and page.html == ""
    assert page.markdown.startswith("# Example Domain")
    assert page.links == ["https://www.iana.org/domains/example", "https://example.com/contact",
                          "https://example.com/about"]
    req = seen[0]
    assert str(req.url) == "https://r.jina.ai/https://example.com/"
    assert req.headers["accept"] == "application/json"
    assert "authorization" not in req.headers


def test_jina_sends_key_only_when_given(serve):
    seen = serve(scrape_more, fixture("jina_reader")["ok"])
    SCRAPE_MORE["jina_reader"](key="jk").fetch("https://example.com/")
    assert seen[0].headers["authorization"] == "Bearer jk"


def test_jina_insufficient_balance_is_quota(serve):
    serve(scrape_more, fixture("jina_reader")["quota"])
    with pytest.raises(QuotaExhausted):
        SCRAPE_MORE["jina_reader"](key="jk").fetch("https://example.com/")


def test_jina_rate_limit_and_target_404_are_provider_errors(serve):
    for case in ("rate_limited", "target_404"):
        serve(scrape_more, fixture("jina_reader")[case])
        with pytest.raises(ProviderError) as e:
            SCRAPE_MORE["jina_reader"]().fetch("https://example.com/missing")
        assert not isinstance(e.value, QuotaExhausted), case


RAW = {
    # name: (expected host, auth param or header, link the fixture html carries)
    "zenrows": ("api.zenrows.com", ("param", "apikey"),
                "https://www.scrapingcourse.com/ecommerce/product/abominable-hoodie/"),
    "scrapingant": ("api.scrapingant.com", ("header", "x-api-key"),
                    "https://www.iana.org/domains/example"),
    "scrape_do": ("api.scrape.do", ("param", "token"), "https://www.iana.org/domains/example"),
    "scraperapi": ("api.scraperapi.com", ("param", "api_key"),
                   "https://www.iana.org/domains/example"),
}


@pytest.mark.parametrize("name", sorted(RAW))
def test_raw_html_scrapers_parse_fixture(serve, name):
    host, (where, field), link = RAW[name]
    seen = serve(scrape_more, fixture(name)["ok"])
    page, units = SCRAPE_MORE[name]("k").fetch("https://example.com/")
    assert units == 1
    assert page.raw_html is True and "<html" in page.html.lower()
    assert link in page.links
    req = seen[0]
    assert req.url.host == host
    assert req.url.params["url"] == "https://example.com/"
    if where == "param":
        assert req.url.params[field] == "k"
    else:
        assert req.headers[field] == "k" and field not in req.url.params
    # cheapest raw-HTML mode: no JS rendering requested
    for flag in ("js_render", "render", "premium", "super"):
        assert req.url.params.get(flag) in (None, "false")
    if name == "scrapingant":
        assert req.url.params["browser"] == "false"


@pytest.mark.parametrize("name", sorted(RAW))
def test_raw_html_scrapers_out_of_credits_is_quota(serve, name):
    serve(scrape_more, fixture(name)["quota"])
    with pytest.raises(QuotaExhausted) as e:
        SCRAPE_MORE[name]("k").fetch("https://example.com/")
    assert not isinstance(e.value, BillingSignal)


@pytest.mark.parametrize("name", sorted(RAW))
def test_raw_html_scrapers_target_404_is_not_quota(serve, name):
    serve(scrape_more, fixture(name)["target_404"])
    with pytest.raises(ProviderError) as e:
        SCRAPE_MORE[name]("k").fetch("https://example.com/missing")
    assert not isinstance(e.value, QuotaExhausted)


def test_scrapingant_bad_token_is_not_quota_and_target_status_header_checked(serve):
    serve(scrape_more, fixture("scrapingant")["bad_token"])
    with pytest.raises(ProviderError) as e:
        SCRAPE_MORE["scrapingant"]("k").fetch("https://example.com/")
    assert not isinstance(e.value, QuotaExhausted)
    ok = dict(fixture("scrapingant")["ok"])
    ok["headers"] = {**ok["headers"], "Ant-page-status-code": "404"}
    serve(scrape_more, ok)
    with pytest.raises(ProviderError):
        SCRAPE_MORE["scrapingant"]("k").fetch("https://example.com/missing")


def test_units_follow_provider_cost_headers(serve):
    spec = dict(fixture("zenrows")["ok"])
    spec["headers"] = {**spec["headers"], "X-Request-Credits": "10"}
    serve(scrape_more, spec)
    assert SCRAPE_MORE["zenrows"]("k").fetch("https://example.com/")[1] == 10
