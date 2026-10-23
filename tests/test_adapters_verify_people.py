"""Verify + people adapters, driven by the providers' own documented example responses."""
import json
from pathlib import Path

import httpx
import pytest

from leadgen.providers import people as people_mod
from leadgen.providers import verify_more as vm
from leadgen.providers.base import ProviderError, QuotaExhausted

FIX = Path(__file__).parent / "fixtures" / "providers"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def mock(monkeypatch, module, handler, seen=None):
    def h(request):
        if seen is not None:
            seen.append(request)
        return handler(request)
    monkeypatch.setattr(module, "http_client",
                        lambda timeout=60.0: httpx.Client(transport=httpx.MockTransport(h)))


def reply(status, body):
    return lambda req: httpx.Response(status, json=body)


# ------------------------------------------------------------------ verify: fixtures

@pytest.mark.parametrize("name, cls, email, verdict, units", [
    ("mailboxvalidator", vm.MailboxValidator, "jane.doe@example.com", "ok", 1),
    ("zerobounce", vm.ZeroBounce, "john.doe@zerobounce.net", "ok", 1),
    ("abstract_email", vm.AbstractEmail, "johnsmith@gmail.com", "ok", 1),
    ("reoon", vm.Reoon, "jhon123@gmail.com", "ok", 1),
    ("hunter_verify", vm.HunterVerify, "patrick@stripe.com", "ok", 1),
    ("getprospect_verify", vm.GetProspectVerify, "emily.carter.sample@intercom.com", "ok", 1),
])
def test_verify_parses_docs_example(monkeypatch, name, cls, email, verdict, units):
    seen = []
    mock(monkeypatch, vm, reply(200, fixture(name)["response"]), seen)
    assert cls(key="test-key").verify(email) == (verdict, units)
    assert len(seen) == 1


def test_reoon_uses_power_mode(monkeypatch):
    seen = []
    mock(monkeypatch, vm, reply(200, fixture("reoon")["response"]), seen)
    vm.Reoon(key="k").verify("jhon123@gmail.com")
    assert seen[0].url.params["mode"] == "power"


def test_getprospect_verify_posts_json_with_header(monkeypatch):
    seen = []
    mock(monkeypatch, vm, reply(200, fixture("getprospect_verify")["response"]), seen)
    vm.GetProspectVerify(key="k").verify("a@b.com")
    assert seen[0].method == "POST" and seen[0].headers["x-api-key"] == "k"
    assert json.loads(seen[0].content) == {"data": {"email": "a@b.com"}}


# ------------------------------------------------------------------ verify: mappings

@pytest.mark.parametrize("status, sub, verdict, units", [
    ("valid", "", "ok", 1), ("catch-all", "", "catch_all", 1), ("invalid", "mailbox_not_found", "invalid", 1),
    ("unknown", "greylisted", "unknown", 0), ("spamtrap", "", "invalid", 1), ("abuse", "", "invalid", 1),
    ("do_not_mail", "global_suppression", "invalid", 1), ("do_not_mail", "disposable", "disposable", 1),
    ("do_not_mail", "role_based", "unknown", 1), ("do_not_mail", "role_based_catch_all", "catch_all", 1),
    ("something_new", "", "unknown", 1),
])
def test_zerobounce_vocabulary(monkeypatch, status, sub, verdict, units):
    body = dict(fixture("zerobounce")["response"], status=status, sub_status=sub)
    mock(monkeypatch, vm, reply(200, body))
    assert vm.ZeroBounce(key="k").verify("x@y.com") == (verdict, units)


def test_abstract_deliverable_without_smtp_is_unknown(monkeypatch):
    body = fixture("abstract_email")["response"]
    body["is_smtp_valid"] = {"value": None, "text": "UNKNOWN"}
    mock(monkeypatch, vm, reply(200, body))
    assert vm.AbstractEmail(key="k").verify("x@y.com") == ("unknown", 1)


@pytest.mark.parametrize("patch, verdict", [
    ({"status": "role_account", "is_deliverable": True, "is_safe_to_send": True}, "ok"),
    ({"status": "role_account", "is_deliverable": False}, "unknown"),
    ({"status": "catch_all"}, "catch_all"), ({"status": "disabled"}, "invalid"),
    ({"status": "inbox_full"}, "unknown"), ({"status": "spamtrap"}, "invalid"),
    ({"status": "valid", "verification_mode": "quick"}, "unknown"),
])
def test_reoon_vocabulary(monkeypatch, patch, verdict):
    body = dict(fixture("reoon")["response"], **patch)
    mock(monkeypatch, vm, reply(200, body))
    assert vm.Reoon(key="k").verify("x@y.com")[0] == verdict


@pytest.mark.parametrize("patch, verdict", [
    ({"is_catchall": True}, "catch_all"), ({"is_disposable": True}, "disposable"),
    ({"is_verified": False, "status": False}, "invalid"),
    ({"is_verified": None, "status": False, "is_greylisted": True}, "unknown"),
])
def test_mailboxvalidator_vocabulary(monkeypatch, patch, verdict):
    body = dict(fixture("mailboxvalidator")["response"], **patch)
    mock(monkeypatch, vm, reply(200, body))
    assert vm.MailboxValidator(key="k").verify("x@y.com")[0] == verdict


@pytest.mark.parametrize("status, verdict", [("accept_all", "catch_all"), ("webmail", "unknown"),
                                             ("disposable", "disposable"), ("invalid", "invalid")])
def test_hunter_verify_vocabulary(monkeypatch, status, verdict):
    body = fixture("hunter_verify")["response"]
    body["data"]["status"] = status
    mock(monkeypatch, vm, reply(200, body))
    assert vm.HunterVerify(key="k").verify("x@y.com") == (verdict, 1)


def test_hunter_verify_202_pending_is_unknown(monkeypatch):
    mock(monkeypatch, vm, reply(202, {"data": None}))
    assert vm.HunterVerify(key="k").verify("x@y.com") == ("unknown", 1)


@pytest.mark.parametrize("status, verdict, units", [("accept_all", "catch_all", 0), ("unknown", "unknown", 0),
                                                    ("invalid", "invalid", 1)])
def test_getprospect_verify_vocabulary(monkeypatch, status, verdict, units):
    body = fixture("getprospect_verify")["response"]
    body["data"]["status"] = status
    mock(monkeypatch, vm, reply(200, body))
    assert vm.GetProspectVerify(key="k").verify("x@y.com") == (verdict, units)


# ------------------------------------------------------------------ verify: out of credits

@pytest.mark.parametrize("name, cls, status, body", [
    ("mailboxvalidator", vm.MailboxValidator, 401, fixture("mailboxvalidator")["errors"]["insufficient_credits"]["body"]),
    ("zerobounce", vm.ZeroBounce, 200, fixture("zerobounce")["errors"]["out_of_credits"]["body"]),
    ("abstract_email", vm.AbstractEmail, 422, {"error": {"message": "Quota reached"}}),
    ("reoon", vm.Reoon, 200, {"status": "error", "reason": "Not enough credits remaining today."}),
    ("hunter_verify", vm.HunterVerify, 429, fixture("hunter_verify")["errors"]["usage_limit"]["body"]),
    ("getprospect_verify", vm.GetProspectVerify, 402, fixture("getprospect_verify")["errors"]["payment_required"]["body"]),
])
def test_verify_out_of_credits_is_quota(monkeypatch, name, cls, status, body):
    mock(monkeypatch, vm, reply(status, body))
    with pytest.raises(QuotaExhausted):
        cls(key="k").verify("x@y.com")


@pytest.mark.parametrize("cls, status", [(vm.AbstractEmail, 429), (vm.HunterVerify, 403),
                                         (vm.GetProspectVerify, 408)])
def test_verify_throttle_is_not_quota(monkeypatch, cls, status):
    mock(monkeypatch, vm, reply(status, {"errors": [{"name": "RATE_LIMIT"}]}))
    with pytest.raises(ProviderError) as ei:
        cls(key="k").verify("x@y.com")
    assert not isinstance(ei.value, QuotaExhausted)


def test_registry_keys():
    assert set(vm.VERIFY_MORE) == {"mailboxvalidator", "zerobounce", "abstract_email", "reoon",
                                   "hunter_verify", "getprospect_verify"}
    assert set(people_mod.PEOPLE) == {"getprospect", "hunter_domain", "tomba", "apollo"}


# ------------------------------------------------------------------ people: fixtures

def test_getprospect_people(monkeypatch):
    seen = []
    mock(monkeypatch, people_mod, reply(200, fixture("getprospect")["response"]), seen)
    persons, units = people_mod.GetProspect(key="k").find("intercom.com")
    assert units == 1 and len(persons) == 1
    p = persons[0]
    assert (p.email, p.first_name, p.last_name, p.verified) == (
        "emily.carter.sample@intercom.com", "Emily", "Carter", True)
    assert p.position == "Senior Product Marketing Manager"
    sent = json.loads(seen[0].content)
    assert sent["filters"]["company"]["domain"]["include"] == ["intercom.com"]
    assert sent["enrich_email"] is True


def test_hunter_domain_people(monkeypatch):
    seen = []
    mock(monkeypatch, people_mod, reply(200, fixture("hunter_domain")["response"]), seen)
    persons, units = people_mod.HunterDomain(key="k").find("https://www.intercom.com/")
    assert units == 1 and [p.email for p in persons] == ["ciaran@intercom.com"]
    p = persons[0]
    assert (p.first_name, p.last_name, p.position, p.confidence, p.verified) == (
        "Ciaran", "Lee", "Support Engineer", 92, True)
    assert seen[0].url.params["domain"] == "intercom.com"
    assert seen[0].url.params["provenance"] == "found"


def test_hunter_domain_drops_inferred_and_off_domain(monkeypatch):
    body = fixture("hunter_domain")["response"]
    base = body["data"]["emails"][0]
    body["data"]["emails"] = [dict(base, provenance="inferred"),
                              dict(base, value="ciaran@other.com"),
                              dict(base, verification={"status": "accept_all"})]
    mock(monkeypatch, people_mod, reply(200, body))
    persons, _ = people_mod.HunterDomain(key="k").find("intercom.com")
    assert len(persons) == 1 and persons[0].verified is False


def test_hunter_domain_empty_costs_nothing(monkeypatch):
    mock(monkeypatch, people_mod, reply(200, {"data": {"emails": []}, "meta": {"results": 0}}))
    assert people_mod.HunterDomain(key="k").find("example.com") == ([], 0)


def test_tomba_people(monkeypatch):
    seen = []
    mock(monkeypatch, people_mod, reply(200, fixture("tomba")["response"]), seen)
    persons, units = people_mod.Tomba(key="ta_x:ts_y").find("stripe.com")
    assert units == 1
    assert [(p.email, p.verified, p.confidence) for p in persons] == [
        ("jane.doe@stripe.com", True, 100), ("john.smith@stripe.com", False, 76)]
    assert seen[0].headers["X-Tomba-Key"] == "ta_x" and seen[0].headers["X-Tomba-Secret"] == "ts_y"


def test_tomba_requires_secret():
    with pytest.raises(ProviderError):
        people_mod.Tomba(key="only-a-key")


def test_apollo_people(monkeypatch):
    fx = fixture("apollo")["response"]
    seen = []

    def handler(req):
        if req.url.path.endswith("/mixed_people/api_search"):
            return httpx.Response(200, json=fx["search"])
        return httpx.Response(200, json=fx["bulk_match"])
    mock(monkeypatch, people_mod, handler, seen)
    persons, units = people_mod.Apollo(key="k").find("apollo.io")
    assert units == 3
    assert [p.email for p in persons] == ["marcus.ellery@apollo.io", "brian.calderwood@apollo.io",
                                          "nadia.calloway@apollo.io"]
    assert all(p.verified for p in persons)
    assert persons[0].position == "VP of Sales Operations"
    assert seen[0].url.params["q_organization_domains_list[]"] == "apollo.io"
    assert len(json.loads(seen[1].content)["details"]) == 3


def test_apollo_off_domain_dropped(monkeypatch):
    fx = fixture("apollo")["response"]
    mock(monkeypatch, people_mod, lambda req: httpx.Response(
        200, json=fx["search"] if "api_search" in req.url.path else fx["bulk_match"]))
    persons, _ = people_mod.Apollo(key="k").find("stripe.com")
    assert persons == []


# ------------------------------------------------------------------ people: out of credits / plan

@pytest.mark.parametrize("cls, key, status, body", [
    (people_mod.GetProspect, "k", 402, fixture("getprospect")["errors"]["payment_required"]["body"]),
    (people_mod.HunterDomain, "k", 429, fixture("hunter_domain")["errors"]["usage_limit"]["body"]),
    (people_mod.Tomba, "a:b", 402, fixture("tomba")["errors"]["search_quota_exceeded"]["body"]),
    (people_mod.Tomba, "a:b", 429, fixture("tomba")["errors"]["rate_limit"]["body"]),
    (people_mod.Apollo, "k", 429, fixture("apollo")["errors"]["rate_limited_429"]["body"]),
])
def test_people_out_of_credits_is_quota(monkeypatch, cls, key, status, body):
    mock(monkeypatch, people_mod, reply(status, body))
    with pytest.raises(QuotaExhausted):
        cls(key=key).find("example.com")


def test_apollo_plan_refusal_is_quota(monkeypatch):
    body = fixture("apollo")["errors"]["plan_refused_403"]["body"]
    mock(monkeypatch, people_mod, reply(403, body))
    with pytest.raises(QuotaExhausted, match="plan refused"):
        people_mod.Apollo(key="k").find("example.com")


def test_apollo_plan_refusal_on_enrich_step(monkeypatch):
    fx = fixture("apollo")
    mock(monkeypatch, people_mod, lambda req: (
        httpx.Response(200, json=fx["response"]["search"]) if "api_search" in req.url.path
        else httpx.Response(422, json={"error": "This feature is not available on your plan."})))
    with pytest.raises(QuotaExhausted, match="plan refused"):
        people_mod.Apollo(key="k").find("apollo.io")
