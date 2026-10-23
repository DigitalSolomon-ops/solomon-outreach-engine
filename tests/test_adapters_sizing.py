import json
from pathlib import Path

import httpx
import pytest

from leadgen.providers import QuotaExhausted, ProviderError
from leadgen.providers import sizing

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "census_cbp.json"


def _patch(monkeypatch, handler, follow=False):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(sizing, "http_client", lambda timeout=60.0: httpx.Client(
        transport=httpx.MockTransport(wrapped), follow_redirects=follow))
    return seen


def test_parses_fixture(monkeypatch):
    body = FIXTURE.read_text(encoding="utf-8")
    seen = _patch(monkeypatch, lambda r: httpx.Response(200, text=body))
    count, units = sizing.CensusCbp().establishments("238220", "37", "071")
    assert (count, units) == (123, 1)
    q = seen[0].url.params
    assert seen[0].url.path == f"/data/{sizing.latest_vintage}/cbp"
    assert q["NAICS2017"] == "238220" and q["for"] == "county:071" and q["in"] == "state:37"
    assert "ESTAB" in q["get"]
    assert "key" not in q                       # keyless when no key given


def test_sends_key_only_when_given(monkeypatch):
    body = FIXTURE.read_text(encoding="utf-8")
    seen = _patch(monkeypatch, lambda r: httpx.Response(200, text=body))
    sizing.CensusCbp(key="k123").establishments("23", "45", "091")
    assert seen[0].url.params["key"] == "k123"


def test_picks_all_sizes_row_when_breakdown_returned(monkeypatch):
    data = [["ESTAB", "NAICS2017", "EMPSZES", "LFO", "state", "county"],
            ["7", "238220", "212", "001", "37", "071"],
            ["40", "238220", "001", "001", "37", "071"]]
    _patch(monkeypatch, lambda r: httpx.Response(200, json=data))
    assert sizing.CensusCbp().establishments("238220", "37", "071") == (40, 1)


def test_204_is_none(monkeypatch):
    _patch(monkeypatch, lambda r: httpx.Response(204))
    assert sizing.CensusCbp().establishments("238220", "37", "071") == (None, 1)


def _key_redirect(page):
    def handler(request):
        if request.url.path.endswith(".html"):
            return httpx.Response(200, text="<html><title>Invalid Key</title></html>")
        return httpx.Response(302, headers={"X-DataWebAPI-KeyError": "1",
                                            "Location": f"https://api.census.gov/data/{page}"})
    return handler


@pytest.mark.parametrize("follow", [False, True])
@pytest.mark.parametrize("page", ["invalid_key.html", "missing_key.html"])
def test_key_error_is_quota(monkeypatch, page, follow):
    _patch(monkeypatch, _key_redirect(page), follow=follow)
    with pytest.raises(QuotaExhausted):
        sizing.CensusCbp(key="bad").establishments("238220", "37", "071")


def test_bad_variable_is_provider_error(monkeypatch):
    _patch(monkeypatch, lambda r: httpx.Response(400, text="error: unknown variable 'ESTABX'"))
    with pytest.raises(ProviderError) as exc:
        sizing.CensusCbp().establishments("238220", "37", "071")
    assert not isinstance(exc.value, QuotaExhausted)


def test_rate_limit_is_quota(monkeypatch):
    _patch(monkeypatch, lambda r: httpx.Response(429, text="Too Many Requests"))
    with pytest.raises(QuotaExhausted):
        sizing.CensusCbp().establishments("238220", "37", "071")


def test_rejects_bad_naics():
    with pytest.raises(ProviderError):
        sizing.CensusCbp().establishments("2382201", "37", "071")


def test_registry():
    from leadgen.providers import SIZING
    assert SIZING["census_cbp"] is sizing.CensusCbp


@pytest.mark.parametrize("geo,want", [
    ("Gastonia, NC", ("37", "071")), ("Charlotte, NC", ("37", "119")),
    ("Belmont, NC", ("37", "071")), ("Mount Holly, NC", ("37", "071")),
    ("Mt. Holly NC", ("37", "071")), ("Concord, NC", ("37", "025")),
    ("Rock Hill, SC", ("45", "091")), ("Huntersville, NC", ("37", "119")),
    ("Matthews, NC", ("37", "119")), ("Kannapolis, NC", ("37", "025")),
    ("Mooresville, NC", ("37", "097")), ("Fort Mill, SC", ("45", "091")),
    ("Monroe, NC", ("37", "179")), ("Lincolnton, NC", ("37", "109")),
    ("Shelby, NC", ("37", "045")), ("Salisbury, North Carolina", ("37", "159")),
    ("Atlanta, GA", None), ("Concord, SC", None), ("", None),
])
def test_city_to_county(geo, want):
    assert sizing.city_to_county(geo) == want


def test_county_table_covers_lancaster():
    assert sizing.COUNTY_FIPS["Lancaster County, SC"] == ("45", "057")
    assert len(sizing.COUNTY_FIPS) == 10


def test_fixture_is_array_of_arrays():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(data, list) and all(isinstance(r, list) for r in data)
