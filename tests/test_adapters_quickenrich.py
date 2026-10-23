import json
from pathlib import Path

import httpx
import pytest

import leadgen.providers.firecrawl_usage as fu
import leadgen.providers.quickenrich as qe
from conftest import FakeSecrets
from leadgen.providers import ProviderError, QuotaExhausted
from leadgen.providers.discovery import FirecrawlSearch
from leadgen.rotation import NoProviderAvailable, Rotation

FX = json.loads((Path(__file__).parent / "fixtures/providers/quickenrich.json").read_text(encoding="utf-8"))


def client_for(monkeypatch, module, status, body, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)
    monkeypatch.setattr(module, "http_client", lambda timeout=60: httpx.Client(transport=httpx.MockTransport(handler)))


def test_domain_search_parses_unverified_people(monkeypatch):
    seen = []
    client_for(monkeypatch, qe, 200, FX["ok"], seen)
    a = qe.QuickEnrich("k")
    people, units = a.find("example.com")
    assert [p.email for p in people] == ["jane.doe@example.com", "john.smith@example.com"]
    assert people[0].first_name == "Jane" and people[0].verified is None and units == 2
    assert a.remaining_credits == 98
    assert seen[0].headers["Authorization"] == "Bearer k" and seen[0].url.params["company_url"] == "example.com"


def test_not_found_is_free(monkeypatch):
    client_for(monkeypatch, qe, 200, FX["not_found"])
    assert qe.QuickEnrich("k").find("acme.com") == ([], 0)


def test_off_domain_dropped(monkeypatch):
    client_for(monkeypatch, qe, 200, FX["ok"])
    assert qe.QuickEnrich("k").find("other.com")[0] == []


def test_errors(monkeypatch):
    client_for(monkeypatch, qe, 401, FX["error_401"])
    with pytest.raises(QuotaExhausted):
        qe.QuickEnrich("k").find("example.com")
    client_for(monkeypatch, qe, 429, FX["error_429"])
    with pytest.raises(ProviderError) as e:
        qe.QuickEnrich("k").find("example.com")
    assert not isinstance(e.value, QuotaExhausted)          # a rate limit never parks it for the month
    client_for(monkeypatch, qe, 402, {"success": False, "message": "Not enough credits", "code": 402})
    with pytest.raises(QuotaExhausted):
        qe.QuickEnrich("k").find("example.com")


def test_firecrawl_shared_account_uses_its_own_remaining_credits(monkeypatch, ledger):
    """The account is shared with other tools: -6 remaining on a 1,000 plan means spent."""
    client_for(monkeypatch, fu, 200, {"success": True, "data": {"remainingCredits": -6}})
    assert FirecrawlSearch("k").usage() == 1006
    entries = [{"name": "firecrawl", "kind": "web", "secret": "k", "cap": 950, "check_usage": True,
                "plan_credits": 1000}]
    rot = Rotation("discovery", entries, {"firecrawl": FirecrawlSearch}, FakeSecrets({"k": "x"}), ledger)
    with pytest.raises(NoProviderAvailable):
        rot.call("search", "q", "g", 1)
    assert "1006/950" in rot.skipped["firecrawl"]
