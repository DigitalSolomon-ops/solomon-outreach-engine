"""Discovery adapters in leadgen.providers.discovery_more, driven by the providers' own docs
examples (tests/fixtures/providers/<name>.json) through an httpx.MockTransport. No network."""
import json
from pathlib import Path

import httpx
import pytest

from leadgen.providers import discovery_more as dm
from leadgen.providers.base import BillingSignal, ProviderError, QuotaExhausted

FIX = Path(__file__).parent / "fixtures" / "providers"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def mock_http(monkeypatch):
    """mock_http(handler) routes every adapter request to handler(request) -> httpx.Response."""
    seen = []

    def install(handler):
        def wrapped(request):
            seen.append(request)
            return handler(request)
        monkeypatch.setattr(dm, "http_client", lambda *a, **k: httpx.Client(
            transport=httpx.MockTransport(wrapped)))
        return seen
    return install


def reply(status, body):
    return lambda request: httpx.Response(status, json=body)


# ------------------------------------------------------------------ google_places

def test_google_places_parses_docs_example(mock_http):
    seen = mock_http(reply(200, fixture("google_places")))
    cands, units = dm.GooglePlaces("k").search("vegetarian food", "Sydney, Australia", 50)
    assert units == 1
    assert [c.name for c in cands] == ["Mother Chu's Vegetarian Kitchen",
                                       "Veggo Sizzle - Vegan & Vegetarian Restaurant, Five Dock, Sydney"]
    assert cands[0].website == "http://www.motherchusvegetarian.com.au/"
    assert cands[0].category == "vegetarian_restaurant" and "restaurant" in cands[0].categories
    req = seen[0]
    assert req.method == "POST" and req.headers["X-Goog-Api-Key"] == "k"
    mask = req.headers["X-Goog-FieldMask"].split(",")
    assert "places.id" in mask and "places.regularOpeningHours.weekdayDescriptions" in mask
    assert json.loads(req.content)["pageSize"] == 20
    assert dm.GooglePlaces.persist == "place_id_only"


def test_google_places_skips_closed_and_maps_transient_fields(mock_http):
    body = {"places": [
        {"id": "ChIJ1", "displayName": {"text": "Open Co"}, "websiteUri": "https://open.example",
         "nationalPhoneNumber": "(704) 555-0100", "rating": 4.7, "userRatingCount": 88,
         "types": ["plumber"], "businessStatus": "OPERATIONAL",
         "regularOpeningHours": {"weekdayDescriptions": ["Monday: 8:00 AM - 5:00 PM"]}},
        {"id": "ChIJ2", "displayName": {"text": "Gone Co"}, "websiteUri": "https://gone.example",
         "businessStatus": "CLOSED_PERMANENTLY"},
        {"id": "ChIJ3", "displayName": {"text": "No Site"}, "businessStatus": "OPERATIONAL"}]}
    mock_http(reply(200, body))
    cands, _ = dm.GooglePlaces("k").search("plumber", "Gastonia, NC", 5)
    assert len(cands) == 1
    c = cands[0]
    assert (c.place_id, c.rating, c.review_count) == ("ChIJ1", 4.7, 88)
    assert c.hours == ["Monday: 8:00 AM - 5:00 PM"] and c.phone == "(704) 555-0100"


def test_google_places_quota_and_billing(mock_http):
    mock_http(reply(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                    "message": "Quota exceeded for quota metric 'Text Search requests'",
                                    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                                 "reason": "RATE_LIMIT_EXCEEDED", "domain": "googleapis.com"}]}}))
    with pytest.raises(QuotaExhausted):
        dm.GooglePlaces("k").search("q", "g", 5)
    mock_http(reply(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                    "message": "This API method requires billing to be enabled.",
                                    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                                 "reason": "BILLING_DISABLED", "domain": "googleapis.com"}]}}))
    with pytest.raises(BillingSignal):
        dm.GooglePlaces("k").search("q", "g", 5)
    mock_http(reply(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                    "message": "The provided API key is invalid.",
                                    "details": [{"reason": "API_KEY_INVALID"}]}}))
    with pytest.raises(ProviderError) as e:
        dm.GooglePlaces("k").search("q", "g", 5)
    assert not isinstance(e.value, QuotaExhausted)


# ------------------------------------------------------------------ outscraper_maps

def test_outscraper_parses_docs_example(mock_http):
    seen = mock_http(reply(200, fixture("outscraper_maps")))
    cands, units = dm.OutscraperMaps("k").search("restaurants", "Manhattan, NY, USA", 3)
    assert units == 3                      # billed per place returned
    assert [c.name for c in cands] == ["Upland", "Scarpetta", "The Smith"]
    up = cands[0]
    assert up.website.startswith("https://uplandnyc.com/")
    assert (up.rating, up.review_count, up.category) == (4.5, 1766, "Californian restaurant")
    assert "Italian restaurant" in up.categories and up.hours[0].startswith("Monday: ")
    req = seen[0]
    assert req.headers["X-API-KEY"] == "k" and req.url.params["async"] == "false"
    assert req.url.params["query"] == "restaurants, Manhattan, NY, USA"


def test_outscraper_402_is_billing(mock_http):
    mock_http(reply(402, {"error": True, "errorMessage":
                          "Please close past due invoices or verify your card information is correct."}))
    with pytest.raises(BillingSignal):
        dm.OutscraperMaps("k").search("q", "g", 5)
    # BillingSignal is a QuotaExhausted: the provider is also stopped for the period.
    with pytest.raises(QuotaExhausted):
        dm.OutscraperMaps("k").search("q", "g", 5)


def test_outscraper_empty_search_still_costs_one_place(mock_http):
    mock_http(reply(200, {"id": "x", "status": "Success", "data": [[]]}))
    assert dm.OutscraperMaps("k").search("q", "g", 5) == ([], 1)


# ------------------------------------------------------------------ hasdata_maps

def test_hasdata_parses_docs_example(mock_http):
    seen = mock_http(reply(200, fixture("hasdata_maps")))
    cands, units = dm.HasDataMaps("k").search("coffee", "Austin, TX", 20)
    assert units == 10                     # 10 credits per Maps Search request
    c = cands[0]
    assert (c.name, c.website, c.place_id) == ("Trippy Buck Coffee", "https://linktr.ee/TrippyBuckCoffee",
                                               "ChIJbz4l-kK1RIYRElA6U66oEQg")
    assert (c.rating, c.review_count, c.category) == (4.9, 39, "Coffee shop")
    assert "Espresso bar" in c.categories and c.hours[0] == "Monday: 8 AM-5 PM"
    assert seen[0].headers["x-api-key"] == "k" and seen[0].url.params["q"] == "coffee Austin, TX"


def test_hasdata_403_is_out_of_credits_but_429_is_not(mock_http):
    mock_http(reply(403, {"message": "You have used up all your API credits."}))
    with pytest.raises(QuotaExhausted):
        dm.HasDataMaps("k").search("q", "g", 5)
    mock_http(reply(429, {"message": "Too Many Requests"}))   # concurrency, transient
    with pytest.raises(ProviderError) as e:
        dm.HasDataMaps("k").search("q", "g", 5)
    assert not isinstance(e.value, QuotaExhausted)


# ------------------------------------------------------------------ foursquare_places

def test_foursquare_parses_schema_fixture(mock_http):
    seen = mock_http(reply(200, fixture("foursquare_places")))
    cands, units = dm.FoursquarePlaces("k").search("hvac", "Gastonia, NC", 10)
    assert units == 1
    assert [c.name for c in cands] == ["Acme Heating & Air"]   # no-site and closed dropped
    c = cands[0]
    assert c.phone == "(704) 555-0100" and c.address.startswith("100 Main St")
    assert c.category.startswith("Heating")
    req = seen[0]
    assert req.headers["Authorization"] == "Bearer k"
    assert req.headers["X-Places-Api-Version"] == "2025-06-17"
    assert "rating" not in req.url.params["fields"] and "hours" not in req.url.params["fields"]
    assert req.url.params["near"] == "Gastonia, NC"


def test_foursquare_quota(mock_http):
    mock_http(reply(429, {"message": "Quota exceeded"}))
    with pytest.raises(QuotaExhausted):
        dm.FoursquarePlaces("k").search("q", "g", 5)


# ------------------------------------------------------------------ geoapify_places

def _geoapify_handler(fx):
    def handler(request):
        if request.url.path.endswith("/geocode/search"):
            return httpx.Response(200, json=fx["geocode"])
        return httpx.Response(200, json=fx["places"])
    return handler


def test_geoapify_parses_fixture_and_drops_siteless(mock_http):
    fx = fixture("geoapify_places")
    seen = mock_http(_geoapify_handler(fx))
    cands, units = dm.GeoapifyPlaces("k").search("electrician", "Tacoma, WA", 50)
    assert units == 2                      # 1 geocode credit + 1 places credit (limit <= 20)
    assert [c.name for c in cands] == ["Tacoma Electric Service"]
    c = cands[0]
    assert c.website == "https://tacomaelectric.example" and c.phone == "+1 253 555 0100"
    assert c.hours == ["Mo-Fr 08:00-17:00"] and "service.electrician" in c.categories
    places_req = seen[1]
    assert places_req.url.params["categories"] == "service.electrician"
    assert places_req.url.params["filter"] == "place:" + fx["geocode"]["results"][0]["place_id"]
    assert places_req.url.params["limit"] == "20"


def test_geoapify_quota(mock_http):
    mock_http(reply(429, {"statusCode": 429, "error": "Too Many Requests", "message": "Limit exceeded"}))
    with pytest.raises(QuotaExhausted):
        dm.GeoapifyPlaces("k").search("electrician", "Tacoma, WA", 5)


# ------------------------------------------------------------------ here_discover

def _here_handler(fx):
    def handler(request):
        if request.url.host.startswith("geocode."):
            return httpx.Response(200, json=fx["geocode"])
        return httpx.Response(200, json=fx["discover"])
    return handler


def test_here_parses_docs_example(mock_http):
    fx = fixture("here_discover")
    seen = mock_http(_here_handler(fx))
    cands, units = dm.HereDiscover("k").search("Statue of liberty", "Berlin", 2)
    assert units == 2                      # geocode + discover transactions
    assert [c.name for c in cands] == ["Statue of Liberty-New York Access", "Statue of Liberty"]
    c = cands[1]
    assert c.website == "http://www.nps.gov/stli" and c.phone == "+12123633200"
    assert c.category == "Historical Monument" and "Museum" in c.categories
    assert seen[1].url.params["at"] == "52.51604,13.37691"
    assert dm.HereDiscover("k").usage() is None   # Usage API is OAuth-only


def test_here_quota_and_billing(mock_http):
    mock_http(reply(429, {"status": 429, "title": "Too Many Requests", "code": "429002",
                          "cause": "Request blocked because too many requests made.",
                          "correlationId": "c", "requestId": "r"}))
    with pytest.raises(QuotaExhausted):
        dm.HereDiscover("k").search("q", "Berlin", 5)
    mock_http(reply(403, {"title": "Forbidden", "status": 403, "code": "403403",
                          "cause": "App credentials do not authorize access because Resource plan attachment has expired.",
                          "action": "Extend the resource plan attachment.",
                          "correlationId": "c", "requestId": "r"}))
    with pytest.raises(BillingSignal):
        dm.HereDiscover("k").search("q", "Berlin", 5)


def test_registry_keys():
    assert set(dm.DISCOVERY_MORE) == {"google_places", "outscraper_maps", "hasdata_maps",
                                      "foursquare_places", "geoapify_places", "here_discover"}
    assert all(cls.kind == "maps" for cls in dm.DISCOVERY_MORE.values())
