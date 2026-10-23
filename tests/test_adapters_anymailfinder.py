import json
from pathlib import Path

import httpx
import pytest

import leadgen.providers.anymailfinder as amf
from leadgen.providers import QuotaExhausted

FX = json.loads((Path(__file__).parent / "fixtures/providers/anymailfinder.json").read_text(encoding="utf-8"))


def client_for(monkeypatch, status, body, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)
    monkeypatch.setattr(amf, "http_client", lambda timeout=60: httpx.Client(transport=httpx.MockTransport(handler)))


def test_company_search_keeps_only_valid_on_domain(monkeypatch):
    seen = []
    client_for(monkeypatch, 200, FX["company_ok"], seen)
    people, units = amf.AnymailFinder("k").find("microsoft.com")
    assert [p.email for p in people] == ["susan.smith@microsoft.com", "john.doe@microsoft.com"]
    assert all(p.verified for p in people) and units == 1
    assert seen[0].headers["Authorization"] == "k" and json.loads(seen[0].content)["domain"] == "microsoft.com"


def test_company_not_found_is_free_and_empty(monkeypatch):
    client_for(monkeypatch, 200, FX["company_not_found"])
    assert amf.AnymailFinder("k").find("acme.com") == ([], 0.0)


def test_off_domain_addresses_dropped(monkeypatch):
    client_for(monkeypatch, 200, {**FX["company_ok"], "valid_emails": ["x@gmail.com"]})
    assert amf.AnymailFinder("k").find("microsoft.com")[0] == []


@pytest.mark.parametrize("status,expect", [("valid", "ok"), ("risky", "unknown"), ("invalid", "invalid"), ("weird", "unknown")])
def test_verify_mapping(monkeypatch, status, expect):
    client_for(monkeypatch, 200, {**FX["verify_ok"], "email_status": status})
    assert amf.AnymailFinderVerify("k").verify("a@b.com") == (expect, 0.2)


@pytest.mark.parametrize("cls,method,arg", [(amf.AnymailFinder, "find", "acme.com"), (amf.AnymailFinderVerify, "verify", "a@acme.com")])
def test_out_of_credits_is_quota(monkeypatch, cls, method, arg):
    client_for(monkeypatch, 402, FX["error_402"])
    with pytest.raises(QuotaExhausted):
        getattr(cls("k"), method)(arg)
    client_for(monkeypatch, 401, FX["error_401"])
    with pytest.raises(QuotaExhausted):
        getattr(cls("k"), method)(arg)


def test_account_credits(monkeypatch):
    client_for(monkeypatch, 200, FX["account"])
    assert amf.AnymailFinderVerify("k").credits_left() == 8698
