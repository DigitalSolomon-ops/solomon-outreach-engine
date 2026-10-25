"""Lead strategies: the registry, the site_filter migration, the API, strategist and writer rules,
and for every new strategy an out-of-credits case, a keep/drop case and a no-guessed-email case."""
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeMaps, FakeScrape
from leadgen import campaigns as camp
from leadgen import strategies
from leadgen.api import create_app
from leadgen.probe import Probe, ProbeLedger, to_markdown, write_report
from leadgen.providers import Candidate, Page, ProviderError, QuotaExhausted, signals
from leadgen.rotation import Rotation
from leadgen.strategist import _apply_strategy, validate_model_plan
from leadgen.writer import validate as validate_draft
from test_engine import always_in_window, make_engine, seed_active  # noqa: F401

NEW = ("fmcsa_new_authority", "sam_new_registrant", "bad_reviews_no_replies", "hiring_now",
       "no_online_booking", "missing_from_ai_answers", "domain_or_ssl_expiring")
LEGACY = {"any": "any_site", "weak": "weak_site", "weak_or_none": "weak_or_no_site", "new_listing": "new_business"}


def mock_http(monkeypatch, handler):
    """Every adapter in signals.py talks through this transport."""
    monkeypatch.setattr(signals, "http_client",
                        lambda *a, **k: httpx.Client(transport=httpx.MockTransport(handler)))


def signal(e, ledger, stage, cls, name="x", secret=None):
    entry = {"name": name}
    if secret:
        e.secrets.values[secret] = "test-key"
        entry["secret"] = secret
    e._signals[stage] = Rotation(stage, [entry], {name: cls}, e.secrets, ledger)


def with_strategy(e, ledger, sid, params=None, **settings_over):
    c = seed_active(e, ledger, daily_cap=settings_over.pop("daily_cap", 3), **settings_over)
    icp = strategies.normalize_icp({**c["icp"], "strategy": sid, "strategy_params": params or {},
                                    "site_filter": strategies.TO_SITE_FILTER.get(sid, "any")})
    ledger.save_campaign(c["id"], {"icp": icp})
    return ledger.campaign(c["id"])


class NoPeople:
    """Any enrichment call fails the test: these strategies must not look anything up."""
    def __init__(self, **_):
        pass

    def find(self, domain):
        raise AssertionError("enrichment was called")


class NoEmailScrape(FakeScrape):
    def fetch(self, url):
        p, n = super().fetch(url)
        p.html = p.html.replace("mailto:info@", "").replace("@", " at ")
        return p, n


# ================================================================ registry
def test_registry_loads_every_strategy_with_a_module():
    reg = strategies.load()
    assert [s["id"] for s in reg["strategies"]][:4] == list(LEGACY.values())
    assert set(NEW) <= set(reg["by_id"]) and len(reg["strategies"]) == 11
    for s in reg["strategies"]:
        obj = strategies._module(s["id"]).STRATEGY(s, {k: v["default"] for k, v in s["params"].items()})
        assert obj.passes and set(obj.passes) <= {"site", "nosite", "source"}


def test_registry_is_schema_validated(tmp_path, registry):
    bad = tmp_path / "s.yaml"
    bad.write_text("strategies:\n  - {id: any_site, label: Any, description: Ten chars plus., source: carrier_pigeon,"
                   " params: {}, facts: [x], cites_site_record: true, needs_preview: allowed,"
                   " uses_industry_list: true, providers: [], status: live}\n", encoding="utf-8")
    with pytest.raises(strategies.StrategyError, match="source"):
        strategies.load(bad, registry)
    bad.write_text(bad.read_text().replace("carrier_pigeon", "maps").replace("providers: []", "providers: [acme]"))
    with pytest.raises(strategies.StrategyError, match="unknown providers"):
        strategies.load(bad, registry)


def test_fmcsa_source_is_pinned_to_the_dataset_with_emails():
    with pytest.raises(AssertionError):
        signals.FmcsaCensus(dataset="az4n-8mr2")
    assert signals.FmcsaCensus().dataset == "kjg3-diqy"


# ================================================================ migration
@pytest.mark.parametrize("site_filter,sid", list(LEGACY.items()))
def test_legacy_site_filter_maps_to_its_strategy(site_filter, sid):
    c = {"icp": {"site_filter": site_filter, "cells": []}}
    assert strategies.strategy_id(c) == sid


def test_campaign_saved_before_the_registry_loads_unchanged(settings, ledger):
    e, _ = make_engine(settings, ledger)
    seed = camp.ensure_seed(ledger, settings)
    old = {**seed, "icp": {k: v for k, v in seed["icp"].items() if k not in ("strategy", "strategy_params")}}
    ledger.save_campaign(old["id"], old)
    raw = ledger.campaign(old["id"])
    assert "strategy" not in raw["icp"] or raw["icp"].get("strategy") == "any_site"
    r = TestClient(create_app(lambda: e)).get(f"/campaigns/{old['id']}").json()
    assert r["icp"]["strategy"] == "any_site" and r["strategy_label"] == "Any business with a website"
    assert r["icp"]["site_filter"] == "any" and r["icp"]["cells"] == old["icp"]["cells"]
    assert ledger.campaign(old["id"])["icp"] == raw["icp"]       # reading never rewrites the record


def test_pre_registry_new_listing_campaign_keeps_its_max_reviews(settings):
    c = {"icp": {"site_filter": "new_listing", "max_reviews": 3, "cells": []}}
    assert strategies.strategy_id(c) == "new_business" and strategies.params(c) == {"max_reviews": 3}


def test_a_site_filter_only_writer_still_wins(settings):
    """Code that predates the registry changes site_filter alone; that change must take effect."""
    c = camp.new_campaign(settings, name="R", description="d", landing_url="https://x.example/r",
                          offer={"name": "O", "facts": ["It is free."]})
    assert c["icp"]["strategy"] == "any_site"
    c["icp"]["site_filter"] = "weak"
    assert strategies.strategy_id(c) == "weak_site"


def test_patch_keeps_strategy_and_site_filter_in_step(settings):
    c = camp.new_campaign(settings, name="R", description="d", landing_url="https://x.example/r",
                          offer={"name": "O", "facts": ["It is free."]})
    c = camp.apply_patch(c, {"icp": {"site_filter": "weak"}})               # the alias
    assert (c["icp"]["strategy"], c["icp"]["site_filter"]) == ("weak_site", "weak")
    c = camp.apply_patch(c, {"icp": {"strategy": "no_online_booking"}})
    assert (c["icp"]["strategy"], c["icp"]["site_filter"]) == ("no_online_booking", "any")
    c = camp.apply_patch(c, {"icp": {"strategy": "domain_or_ssl_expiring", "strategy_params": {"domain_days": 30}}})
    assert strategies.params(c) == {"domain_days": 30, "ssl_days": 14}
    with pytest.raises(camp.CampaignError, match="at most 180"):
        camp.apply_patch(c, {"icp": {"strategy_params": {"domain_days": 999}}})
    with pytest.raises(camp.CampaignError, match="not a known strategy"):
        camp.apply_patch(c, {"icp": {"strategy": "telepathy"}})


def test_new_business_params_mirror_the_legacy_max_reviews(settings):
    pv = {"enabled": True, "industry": "roofing", "price_fact": "It is free."}
    c = camp.new_campaign(settings, name="R", description="d", landing_url="https://x.example/r",
                          offer={"name": "O", "facts": ["It is free."]})
    c = camp.apply_patch(c, {"preview": pv, "icp": {"strategy": "new_business", "strategy_params": {"max_reviews": 2}}})
    assert c["icp"]["max_reviews"] == 2 and c["icp"]["site_filter"] == "new_listing"


def test_strategy_fit_with_the_preview_offer(settings):
    c = camp.new_campaign(settings, name="T", description="d", landing_url="https://x.example/t",
                          offer={"name": "O", "facts": ["It is free."]},
                          cells=[{"segment": "New trucking carriers", "geo": "Gastonia, NC", "query": "x"}])
    with pytest.raises(camp.CampaignError, match="Website Launch"):
        camp.apply_patch(c, {"icp": {"strategy": "new_business"}})
    pv = {"enabled": True, "industry": "roofing", "price_fact": "It is free."}
    with pytest.raises(camp.CampaignError, match="no preview template for 'New trucking carriers'"):
        camp.apply_patch(c, {"preview": pv, "icp": {"strategy": "fmcsa_new_authority"}})
    ok = camp.apply_patch(c, {"icp": {"strategy": "fmcsa_new_authority"}})
    assert ok["icp"]["strategy"] == "fmcsa_new_authority"


def test_a_building_strategy_cannot_be_picked(settings, monkeypatch):
    real = strategies.entry
    monkeypatch.setattr(strategies, "entry", lambda sid: {**real(sid), "status": "building"}
                        if sid == "hiring_now" else real(sid))
    c = camp.new_campaign(settings, name="R", description="d", landing_url="https://x.example/r",
                          offer={"name": "O", "facts": ["It is free."]})
    with pytest.raises(camp.CampaignError, match="still being built"):
        camp.apply_patch(c, {"icp": {"strategy": "hiring_now"}})


# ================================================================ API
def test_get_strategies_lists_live_ones_with_labels_and_params(settings, ledger):
    e, _ = make_engine(settings, ledger)
    r = TestClient(create_app(lambda: e)).get("/strategies").json()
    ids = [s["id"] for s in r["strategies"]]
    assert ids[0] == "any_site" and set(NEW) <= set(ids) and r["default"] == "any_site"
    fm = next(s for s in r["strategies"] if s["id"] == "fmcsa_new_authority")
    assert fm["label"] and fm["description"] and fm["uses_industry_list"] is False
    assert fm["params"]["days"] == {"type": "integer", "default": 30, "min": 1, "max": 90,
                                    "label": "Registered within this many days"}


def test_create_with_a_data_segment_strategy_needs_no_industry(settings, ledger):
    e, _ = make_engine(settings, ledger)
    cl = TestClient(create_app(lambda: e))
    body = {"name": "Truckers", "description": "New carriers", "landing_url": "https://digitalsolomon.com/x",
            "strategy": "fmcsa_new_authority", "strategy_params": {"days": 14}}
    assert cl.post("/campaigns", json=body).status_code == 400            # a segment name is required
    r = cl.post("/campaigns", json={**body, "segment": "New trucking carriers", "geos": ["Gastonia, NC"]})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["icp"]["strategy"] == "fmcsa_new_authority" and c["icp"]["strategy_params"] == {"days": 14}
    assert c["icp"]["cells"][0]["segment"] == "New trucking carriers" and "industry" not in c
    assert c["strategy_label"] == "New trucking carriers (FMCSA)"
    listed = cl.get("/campaigns").json()["campaigns"]
    assert next(x for x in listed if x["id"] == c["id"])["strategy_label"] == "New trucking carriers (FMCSA)"


def test_strategy_changes_only_while_draft_or_paused(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = seed_active(e, ledger)
    cl = TestClient(create_app(lambda: e))
    assert cl.patch(f"/campaigns/{c['id']}", json={"icp": {"strategy": "no_online_booking"}}).status_code == 409
    ledger.save_campaign(c["id"], {"status": "paused"})
    r = cl.patch(f"/campaigns/{c['id']}", json={"icp": {"strategy": "no_online_booking"}})
    assert r.status_code == 200 and r.json()["icp"]["strategy"] == "no_online_booking"
    assert r.json()["icp"]["strategy_source"] == "operator"
    assert any(ev["kind"] == "site_filter" and "No online booking" in ev["what"] for ev in ledger.events())


# ================================================================ strategist
def _plan(**over):
    p = {"icp_cells": [{"segment": "HVAC", "geo": "Gastonia, NC", "query": "HVAC company"}],
         "offer": {"name": "Audit", "facts": ["It is free."]}, "source_plan": {},
         "sequence": [{"step": 0, "day": 0, "angle": "a"}], "risks": [], "recommended_connections": []}
    return {**p, **over}


def test_plan_strategy_is_validated_against_the_registry(registry):
    assert validate_model_plan(_plan(strategy="no_online_booking"), registry) == []
    assert any("unknown strategy 'mind_reading'" in p for p in validate_model_plan(_plan(strategy="mind_reading"), registry))


def test_operator_choice_beats_the_strategist(settings):
    c = camp.new_campaign(settings, name="R", description="d", landing_url="https://x.example/r",
                          offer={"name": "O", "facts": ["It is free."]})
    c["plan"] = {}
    picked = _apply_strategy(c, {"strategy": "no_online_booking"})
    assert strategies.strategy_id(picked) == "no_online_booking" and picked["icp"]["strategy_source"] == "strategist"
    mine = camp.apply_patch(c, {"icp": {"strategy": "weak_site", "strategy_source": "operator"}})
    mine["plan"] = {}
    kept = _apply_strategy(mine, {"strategy": "no_online_booking"})
    assert strategies.strategy_id(kept) == "weak_site" and "operator" in kept["plan"]["strategy_note"]
    unfit = _apply_strategy(c, {"strategy": "new_business"})              # needs the preview offer
    assert strategies.strategy_id(unfit) == "any_site" and "Website Launch" in unfit["plan"]["strategy_note"]


# ================================================================ writer fact rules
def _draft(body, used):
    return {"subject": "A quick note", "body": body, "facts_used": [], "evidence_used": used}


def _strat_campaign(settings, sid):
    c = camp.new_campaign(settings, name="R", description="d", landing_url="https://x.example/r",
                          offer={"name": "Audit", "facts": ["It is free."]})
    c["icp"] = strategies.normalize_icp({**c["icp"], "strategy": sid, "site_filter": strategies.TO_SITE_FILTER.get(sid, "any")})
    return c


BODY = ("Hi Acme,\n\nI noticed something while looking at local businesses and wanted to pass it along in case it "
        "is useful to you and the team this month. {{campaign_link}}\n\nMarcus")


def test_writer_may_cite_only_the_strategy_facts(settings):
    c = _strat_campaign(settings, "hiring_now")
    lead = {"name": "Acme", "geo": "Gastonia, NC", "strategy": "hiring_now"}
    ev = {"fact:hiring": "I saw a job posting from Acme for a Detailer.", "fact:no_chat": "No chat.",
          "site:tech:Wix": "Their website uses Wix (CMS)."}
    assert not [p for p in validate_draft(_draft(BODY, ["fact:hiring"]), c, lead, ev, 0, max_words=150, min_words=20)
                if "strategy" in p]
    probs = validate_draft(_draft(BODY, ["fact:no_chat"]), c, lead, ev, 0, max_words=150, min_words=20)
    assert any("does not emit" in p for p in probs)
    probs = validate_draft(_draft(BODY, ["fact:hiring", "site:tech:Wix"]), c, lead, ev, 0, max_words=150, min_words=20)
    assert any("may cite only its facts" in p for p in probs)


def test_new_business_emails_never_say_they_just_opened(settings):
    c = _strat_campaign(settings, "new_business")
    c["preview"] = {"enabled": True}
    lead = {"name": "Acme", "geo": "Gastonia, NC", "nosite": True}
    body = BODY.replace("I noticed something", "Congrats on the new business. I noticed something").replace(
        " {{campaign_link}}", "\n\nWant to see the preview I built for you?")
    probs = validate_draft(_draft(body, ["fact:nosite"]), c, lead, {"fact:nosite": "x"}, 0, max_words=150, min_words=20)
    assert any("just opened" in p for p in probs)


def test_review_count_fact_counts_as_citing_reviews(settings):
    c = _strat_campaign(settings, "bad_reviews_no_replies")
    lead = {"name": "Acme", "geo": "Gastonia, NC"}
    body = BODY.replace("I noticed something", "None of your recent reviews has a reply. I noticed something")
    ev = {"fact:no_review_replies": "None of the 10 most recent reviews on their Google listing has a reply."}
    probs = validate_draft(_draft(body, ["fact:no_review_replies"]), c, lead, ev, 0, max_words=150, min_words=20)
    assert not any("reviews or ratings" in p for p in probs)


# ================================================================ #9 no_online_booking
BOOKING_HTML = '<html><head><script src="/a.js"></script></head><body><a href="https://calendly.com/acme">Book</a>' \
               '<a href="mailto:info@{d}">Email</a></body></html>'


class BookingScrape(FakeScrape):
    def fetch(self, url):
        d = url.split("//")[1].split("/")[0].removeprefix("www.")
        return Page(url=url, html=BOOKING_HTML.format(d=d)), 1


def test_no_online_booking_keeps_sites_without_a_booking_tool(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = with_strategy(e, ledger, "no_online_booking")
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads and all({f["id"] for f in l["facts"]} == {"no_booking"} for l in leads)
    assert all(not k.startswith("site:") for l in leads for k in l["evidence"])   # only its fact


def test_no_online_booking_drops_sites_with_one(settings, ledger):
    e, _ = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": BookingScrape}, e.secrets, ledger)
    c = with_strategy(e, ledger, "no_online_booking")
    res = e.prospect()
    assert res["campaigns"][c["id"]]["first_touches"]["booking_found"] > 0
    assert not ledger.leads(c["id"], "in_sequence")


def test_no_online_booking_parks_text_only_reads(settings, ledger):
    class TextOnly(FakeScrape):
        def fetch(self, url):
            p, n = super().fetch(url)
            return Page(url=url, markdown=p.html, raw_html=False), n
    e, _ = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": TextOnly}, e.secrets, ledger)
    c = with_strategy(e, ledger, "no_online_booking")
    e.prospect()
    assert ledger.leads(c["id"], "site_unassessed") and not ledger.leads(c["id"], "in_sequence")


def test_no_online_booking_out_of_credits_stops_cleanly(settings, ledger):
    from conftest import OutOfCredits
    e, _ = make_engine(settings, ledger, maps=OutOfCredits)
    c = with_strategy(e, ledger, "no_online_booking")
    res = e.prospect()
    assert "no provider" in res["campaigns"][c["id"]]["first_touches"]["stopped"]
    assert not ledger.leads(c["id"])


def test_no_online_booking_never_guesses_an_email(settings, ledger):
    e, _ = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": NoEmailScrape}, e.secrets, ledger)
    c = with_strategy(e, ledger, "no_online_booking")
    e.prospect()
    rows = ledger.leads(c["id"], "no_email")
    assert rows and all(not r.get("email") for r in rows) and not ledger.leads(c["id"], "in_sequence")


# ================================================================ #11 domain_or_ssl_expiring
def test_rdap_adapter_reads_expiration_and_maps_429(monkeypatch):
    exp = (date.today() + timedelta(days=20)).isoformat() + "T04:00:00Z"
    mock_http(monkeypatch, lambda req: httpx.Response(200, json={"events": [
        {"eventAction": "registration", "eventDate": "2001-01-01T00:00:00Z"},
        {"eventAction": "expiration", "eventDate": exp}]}))
    assert signals.Rdap().expiry("acme.com")[0] == date.fromisoformat(exp[:10])
    mock_http(monkeypatch, lambda req: httpx.Response(429, text="slow down"))
    with pytest.raises(QuotaExhausted):
        signals.Rdap().expiry("acme.com")
    mock_http(monkeypatch, lambda req: httpx.Response(404, text="no"))
    assert signals.Rdap().expiry("acme.com") == (None, 1)


def _rdap(days):
    class R:
        def __init__(self, **_):
            pass

        def expiry(self, domain):
            return (date.today() + timedelta(days=days) if days is not None else None), 1
    return R


def test_domain_expiring_keeps_soon_and_drops_far(settings, ledger, monkeypatch):
    monkeypatch.setattr(signals, "cert_expiry", lambda host: None)
    e, _ = make_engine(settings, ledger)
    signal(e, ledger, "domains", _rdap(25))
    c = with_strategy(e, ledger, "domain_or_ssl_expiring")
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads and all({f["id"] for f in l["facts"]} == {"domain_expiring"} for l in leads)
    when = date.today() + timedelta(days=25)
    assert f"{when:%B} {when.day}, {when.year}" in leads[0]["facts"][0]["text"]
    assert leads[0]["domain"] not in leads[0]["facts"][0]["text"]            # no domain names in the email


def test_domain_expiring_drops_domains_renewed_far_ahead(settings, ledger, monkeypatch):
    monkeypatch.setattr(signals, "cert_expiry", lambda host: date.today() + timedelta(days=80))
    e, _ = make_engine(settings, ledger)
    signal(e, ledger, "domains", _rdap(400))
    c = with_strategy(e, ledger, "domain_or_ssl_expiring")
    e.prospect()
    assert not ledger.leads(c["id"], "in_sequence")
    rows = ledger.leads(c["id"], "excluded")
    assert rows and all(l["reason"] == "domain and certificate are not expiring soon" for l in rows)


def test_expired_certificate_is_a_signal(settings, ledger, monkeypatch):
    monkeypatch.setattr(signals, "cert_expiry", lambda host: date.today() - timedelta(days=3))
    e, _ = make_engine(settings, ledger)
    signal(e, ledger, "domains", _rdap(None))
    c = with_strategy(e, ledger, "domain_or_ssl_expiring")
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads and "expired on" in leads[0]["facts"][0]["text"] and leads[0]["facts"][0]["id"] == "ssl_expiring"


def test_domain_expiring_never_guesses_an_email(settings, ledger, monkeypatch):
    monkeypatch.setattr(signals, "cert_expiry", lambda host: None)
    e, _ = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": NoEmailScrape}, e.secrets, ledger)
    signal(e, ledger, "domains", _rdap(10))
    c = with_strategy(e, ledger, "domain_or_ssl_expiring")
    e.prospect()
    assert ledger.leads(c["id"], "no_email") and not ledger.leads(c["id"], "in_sequence")


# ================================================================ #5 fmcsa_new_authority
ROWS = [{"dot_number": "4400002", "legal_name": "BLUE RIDGE HAULING LLC", "dba_name": "", "phy_city": "GASTONIA",
         "phy_state": "NC", "email_address": "dispatch@blueridgehaul.com", "add_date": "20-SEP-26",
         "authorized_for_hire": "true"},
        {"dot_number": "4400001", "legal_name": "NO MAIL TRUCKING", "dba_name": "", "phy_city": "SHELBY",
         "phy_state": "NC", "email_address": "", "add_date": "19-SEP-26", "authorized_for_hire": "true"}]


def socrata(rows=ROWS, with_email=1, status=200):
    def handler(req):
        if status != 200:
            return httpx.Response(status, text="throttled")
        sel = req.url.params.get("$select", "")
        if sel.startswith("add_date,count"):
            return httpx.Response(200, json=[{"add_date": "25-SEP-26", "count": "4"}, {"add_date": "01-JAN-26", "count": "9"}])
        if "count(email_address)" in sel:
            assert "add_date in ('25-SEP-26'" in req.url.params["$where"]          # anchored to the dataset
            assert "authorized_for_hire='true'" in req.url.params["$where"]
            return httpx.Response(200, json=[{"n": str(len(rows)), "with_email": str(with_email)}])
        assert "/kjg3-diqy.json" in str(req.url)
        return httpx.Response(200, json=rows if int(req.url.params.get("$offset", 0)) == 0 else [])
    return handler


def test_fmcsa_adapter_counts_and_maps_429(monkeypatch):
    mock_http(monkeypatch, socrata())
    a = signals.FmcsaCensus()
    assert a.count(["NC"], 30)[0] == {"rows": 2, "with_email": 1}
    assert [r["dot_number"] for r in a.new_carriers(["NC"], 30, 50)[0]] == ["4400002", "4400001"]
    mock_http(monkeypatch, socrata(status=429))
    with pytest.raises(QuotaExhausted):
        signals.FmcsaCensus().count(["NC"], 30)


def _fmcsa_engine(settings, ledger, monkeypatch, **kw):
    mock_http(monkeypatch, socrata(**kw))
    e, _ = make_engine(settings, ledger)
    e.people = Rotation("people", [{"name": "p"}], {"p": NoPeople}, e.secrets, ledger)
    signal(e, ledger, "carriers", signals.FmcsaCensus, "fmcsa_census")
    c = with_strategy(e, ledger, "fmcsa_new_authority")
    return e, c


def test_fmcsa_uses_the_carriers_own_email_and_no_enrichment(settings, ledger, monkeypatch):
    e, c = _fmcsa_engine(settings, ledger, monkeypatch)
    e.prospect()
    lead = ledger.lead(c["id"], "fmcsa-4400002")
    assert lead["status"] == "in_sequence" and lead["email"] == "dispatch@blueridgehaul.com"
    assert lead["email_source"] == "fmcsa:kjg3-diqy:4400002" and lead["nosite"] is True
    assert lead["name"] == "Blue Ridge Hauling LLC" and set(lead["evidence"]) <= {"fact:fmcsa_registered", "email_source"}
    assert "USDOT number 4400002" in lead["facts"][0]["text"]


def test_fmcsa_row_without_an_email_is_dropped_not_guessed(settings, ledger, monkeypatch):
    e, c = _fmcsa_engine(settings, ledger, monkeypatch)
    e.prospect()
    row = ledger.lead(c["id"], "fmcsa-4400001")
    assert row["status"] == "no_email" and not row.get("email")


def test_fmcsa_refuses_an_ingest_with_no_emails(settings, ledger, monkeypatch):
    e, c = _fmcsa_engine(settings, ledger, monkeypatch, with_email=0)
    res = e.prospect()
    assert not ledger.leads(c["id"])
    assert any("count(email_address) is 0" in x for x in res.get("errors", []) + e.report["errors"])


# ================================================================ #6 sam_new_registrant
def sam_body(rows):
    return {"totalRecords": len(rows), "entityData": [
        {"entityRegistration": {"ueiSAM": r["uei"], "legalBusinessName": r["name"], "dbaName": None,
                                "registrationDate": (date.today() - timedelta(days=5)).isoformat()},
         "coreData": {"entityInformation": {"entityURL": r.get("url")},
                      "physicalAddress": {"city": "CHARLOTTE", "stateOrProvinceCode": "NC"}},
         "assertions": {"goodsAndServices": {"primaryNaics": "238220",
                                             "naicsList": [{"sbaSmallBusiness": "Y" if r.get("small", True) else "N"}]}}}
        for r in rows]}


SAM_ROWS = [{"uei": "U1", "name": "QUEEN CITY HVAC LLC", "url": "queencityhvac.com"},
            {"uei": "U2", "name": "NO SITE LLC", "url": None},
            {"uei": "U3", "name": "BIG PRIME CORP", "url": "https://bigprime.com", "small": False}]


def test_sam_adapter_parses_and_maps_429(monkeypatch):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json=sam_body(SAM_ROWS)))
    data, _ = signals.SamEntity(key="k").new_registrants("NC", 30, 0)
    assert [(r["uei"], r["small"], r["url"]) for r in data["rows"]] == [
        ("U1", True, "queencityhvac.com"), ("U2", True, ""), ("U3", False, "https://bigprime.com")]
    mock_http(monkeypatch, lambda req: httpx.Response(429, text="limit"))
    with pytest.raises(QuotaExhausted):
        signals.SamEntity(key="k").new_registrants("NC", 30, 0)


def test_sam_keeps_small_registrants_with_a_site_and_never_guesses(settings, ledger, monkeypatch):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json=sam_body(SAM_ROWS)))
    e, _ = make_engine(settings, ledger)
    signal(e, ledger, "registrants", signals.SamEntity, "sam_entity", secret="SAM-Gov-API-Key")
    c = with_strategy(e, ledger, "sam_new_registrant")
    e.prospect()
    lead = ledger.lead(c["id"], "queencityhvac.com")
    assert lead["status"] == "in_sequence" and lead["email"].endswith("@queencityhvac.com")
    assert [f["id"] for f in lead["facts"]] == ["sam_registered"] and "NAICS code 238220" in lead["facts"][0]["text"]
    nosite = ledger.lead(c["id"], "sam-U2")
    assert nosite["status"] == "no_email" and not nosite.get("email")
    assert ledger.lead(c["id"], "bigprime.com") is None                    # not a small business


# ================================================================ #8 hiring_now
def test_indeed_adapter_parses_and_maps_403(monkeypatch):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json={"jobs": [
        {"title": "Detailer", "company": "Shine Co", "isoDate": "2026-10-20T00:00:00Z", "sponsored": "False"},
        {"title": "No company"}]}))
    jobs, units = signals.HasDataIndeed(key="k").jobs("auto detailing", "Gastonia, NC")
    assert units == 10 and jobs == [{"title": "Detailer", "company": "Shine Co", "location": "",
                                     "posted": "2026-10-20", "sponsored": False}]
    mock_http(monkeypatch, lambda req: httpx.Response(403, text="no credits"))
    with pytest.raises(QuotaExhausted):
        signals.HasDataIndeed(key="k").jobs("x", "y")


def _jobs(posted_days_ago=3, sponsored=False):
    class J:
        def __init__(self, **_):
            pass

        def jobs(self, keyword, location):
            return [{"title": "Lead Technician", "company": "Bobs Plumbing", "location": location,
                     "posted": (date.today() - timedelta(days=posted_days_ago)).isoformat(),
                     "sponsored": sponsored}], 10
    return J


def test_hiring_now_matches_postings_to_listings(settings, ledger):
    e, _ = make_engine(settings, ledger)
    signal(e, ledger, "jobs", _jobs())
    c = with_strategy(e, ledger, "hiring_now")
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads and leads[0]["facts"] == [{"id": "hiring", "pillar": "Operations",
                                            "text": "I saw a job posting from Bobs Plumbing for a Lead Technician."}]


def test_hiring_now_drops_stale_postings(settings, ledger):
    e, _ = make_engine(settings, ledger)
    signal(e, ledger, "jobs", _jobs(posted_days_ago=90))
    c = with_strategy(e, ledger, "hiring_now")
    e.prospect()
    assert not ledger.leads(c["id"])


def test_hiring_now_never_guesses_an_email(settings, ledger):
    e, _ = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": NoEmailScrape}, e.secrets, ledger)
    signal(e, ledger, "jobs", _jobs())
    c = with_strategy(e, ledger, "hiring_now")
    e.prospect()
    assert ledger.leads(c["id"], "no_email") and not ledger.leads(c["id"], "in_sequence")


# ================================================================ #7 bad_reviews_no_replies
class LowRated(FakeMaps):
    def search(self, query, geo, limit):
        cands, n = super().search(query, geo, limit)
        for i, x in enumerate(cands):
            x.rating, x.place_id = (3.4 if geo.startswith("Gastonia") else 4.8), f"ChIJ{geo[:3]}{i}"
        return cands, n


def _reviews(has_reply=False, calls=None):
    class R:
        def __init__(self, **_):
            pass

        def recent_replies(self, place_id, n):
            if calls is not None:
                calls.append(place_id)
            return [{"has_reply": has_reply and i == 0, "date": "2026-10-01"} for i in range(n)], 10
    return R


def test_reviews_adapter_keeps_only_reply_flags_and_maps_403(monkeypatch):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json={"reviews": [
        {"snippet": "Terrible", "user": {"name": "Pat"}, "isoDate": "2026-10-09T00:00:00Z",
         "response": {"snippet": "Sorry"}},
        {"snippet": "Bad", "user": {"name": "Sam"}, "isoDate": "2026-10-01T00:00:00Z"}]}))
    out, _ = signals.HasDataReviews(key="k").recent_replies("ChIJ1", 10)
    assert out == [{"has_reply": True, "date": "2026-10-09"}, {"has_reply": False, "date": "2026-10-01"}]
    mock_http(monkeypatch, lambda req: httpx.Response(403, text="out of credits"))
    with pytest.raises(QuotaExhausted):
        signals.HasDataReviews(key="k").recent_replies("ChIJ1", 10)


def test_bad_reviews_keeps_low_rated_unreplied_and_reads_reviews_only_for_them(settings, ledger):
    calls = []
    e, _ = make_engine(settings, ledger, maps=LowRated)
    signal(e, ledger, "reviews", _reviews(calls=calls))
    c = with_strategy(e, ledger, "bad_reviews_no_replies")
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads and leads[0]["facts"][0]["id"] == "no_review_replies"
    assert "None of the 10 most recent reviews" in leads[0]["facts"][0]["text"]
    assert all(not l.get("reviews") and "maps:rating" not in l["evidence"] for l in leads)   # nothing else cited
    assert calls and all(pid.startswith("ChIJGas") for pid in calls)     # the 4.8-rated listings cost nothing
    assert any(l["reason"] == "rated 4 or higher" for l in ledger.leads(c["id"], "excluded"))


def test_bad_reviews_drops_owners_who_reply(settings, ledger):
    e, _ = make_engine(settings, ledger, maps=LowRated)
    signal(e, ledger, "reviews", _reviews(has_reply=True))
    c = with_strategy(e, ledger, "bad_reviews_no_replies")
    e.prospect()
    assert not ledger.leads(c["id"], "in_sequence")
    assert any(l["reason"] == "the owner replies to reviews" for l in ledger.leads(c["id"], "excluded"))


def test_bad_reviews_never_guesses_an_email(settings, ledger):
    e, _ = make_engine(settings, ledger, maps=LowRated)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": NoEmailScrape}, e.secrets, ledger)
    signal(e, ledger, "reviews", _reviews())
    c = with_strategy(e, ledger, "bad_reviews_no_replies")
    e.prospect()
    assert ledger.leads(c["id"], "no_email") and not ledger.leads(c["id"], "in_sequence")


# ================================================================ #10 missing_from_ai_answers
def test_linkup_answer_adapter_and_credit_exhaustion(monkeypatch):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json={"answer": "Try **Shine Co**.", "sources": [{"name": "Yelp"}]}))
    assert signals.LinkupAnswer(key="k").answer("best?")[0] == {"answer": "Try **Shine Co**.", "sources": ["Yelp"]}
    mock_http(monkeypatch, lambda req: httpx.Response(429, json={"error": {"code": "INSUFFICIENT_FUNDS_CREDITS"}}))
    with pytest.raises(QuotaExhausted):
        signals.LinkupAnswer(key="k").answer("best?")


def _answer(text):
    class A:
        def __init__(self, **_):
            pass

        def answer(self, question):
            return {"answer": text, "sources": []}, 1
    return A


def test_missing_from_ai_answers_keeps_the_unnamed_and_stores_the_evidence(settings, ledger):
    e, _ = make_engine(settings, ledger)
    # FakeMaps names businesses "Acme <query> | <geo>"; the answer names one other business only
    signal(e, ledger, "answer", _answer("The best are **Zenith Air** and **Polar Comfort**."))
    c = with_strategy(e, ledger, "missing_from_ai_answers")
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads
    f = leads[0]["facts"][0]
    assert f["id"] == "ai_answer_absent" and "did not mention your business" in f["text"]
    assert f["evidence"]["names"] == ["Zenith Air", "Polar Comfort"] and f["evidence"]["provider"] == "x"
    assert f["evidence"]["query"].startswith("What are the best ")


def test_missing_from_ai_answers_drops_businesses_the_answer_named(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c0 = seed_active(e, ledger)
    q = c0["icp"]["cells"][0]["query"]
    signal(e, ledger, "answer", _answer(f"Top picks: **Acme {q}** for most jobs."))
    c = with_strategy(e, ledger, "missing_from_ai_answers")
    e.prospect()
    named = [l for l in ledger.leads(c["id"], "excluded") if l["reason"] == "named in the AI answer"]
    assert named and not ledger.leads(c["id"], "in_sequence")


def test_missing_from_ai_answers_never_guesses_an_email(settings, ledger):
    e, _ = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": NoEmailScrape}, e.secrets, ledger)
    signal(e, ledger, "answer", _answer("**Zenith Air**"))
    c = with_strategy(e, ledger, "missing_from_ai_answers")
    e.prospect()
    assert ledger.leads(c["id"], "no_email") and not ledger.leads(c["id"], "in_sequence")


# ================================================================ probe
def test_probe_counts_without_writing_leads_or_drafts(settings, ledger, tmp_path):
    e, smtp = make_engine(settings, ledger)
    r = Probe(e, "no_online_booking", ["Gastonia, NC", "Charlotte, NC"], n=10).run()
    # two Acme listings plus one directory listing (excluded domain), seen once
    assert (r["candidates"], r["kept"], r["email_found"], r["verified"]) == (3, 2, 2, 2)
    assert r["outcomes"]["excluded"] == 1 and r["enrichment_calls"] == 0
    assert not ledger.leads("probe-no_online_booking") and not ledger.campaign("probe-no_online_booking")
    assert smtp.sent == []
    path = write_report(r, str(tmp_path))
    text = open(path, encoding="utf-8").read()
    assert "| Verified (sendable) | 2 |" in text and "No campaign, no drafts, nothing sent." in text


def test_probe_ledger_keeps_leads_in_memory(ledger):
    pl = ProbeLedger(ledger)
    pl.save_lead("probe-x", "a.com", {"status": "verified"})
    assert pl.lead("probe-x", "a.com")["status"] == "verified" and ledger.lead("probe-x", "a.com") is None
    assert to_markdown({"strategy": "x", "label": "X", "date": "2026-10-25", "geos": ["G"], "n": 1, "segment": "s",
                        "candidates": 60, "kept": 1, "email_found": 0, "verified": 0,
                        "rates": {"kept": None, "email_found": None, "verified": None, "verified_per_candidate": 0},
                        "outcomes": {}, "reasons": {}, "email_sources": {}, "enrichment_calls": 0,
                        "provider_units": {}, "stopped": "", "errors": []}).count("not worth a campaign yet") == 1


_ = (Candidate, ProviderError)


def test_new_business_campaign_script_makes_a_valid_new_business_draft(settings, ledger):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "nbc", Path(__file__).resolve().parent.parent / "scripts" / "create_new_business_campaign.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    it = mod.plan("auto-detailing", 5)
    e, _ = make_engine(settings, ledger)
    cl = TestClient(create_app(lambda: e))
    c = cl.post("/campaigns", json=it["create"]).json()
    r = cl.patch(f"/campaigns/{c['id']}", json=it["patch"])
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["icp"]["strategy"] == "new_business" and c["icp"]["site_filter"] == "new_listing"
    assert c["icp"]["max_reviews"] == 5 and c["status"] == "draft" and c["preview"]["enabled"]


def test_google_nosite_search_reads_past_the_first_page(monkeypatch):
    from leadgen.providers import discovery_more as dm
    pages = {None: ({"places": [{"id": "A", "displayName": {"text": "Has Site"}, "websiteUri": "https://a.com"}],
                     "nextPageToken": "t2"}),
             "t2": ({"places": [{"id": "B", "displayName": {"text": "New Shine"}, "userRatingCount": 2}],
                     "nextPageToken": "t3"}),
             "t3": ({"places": [{"id": "C", "displayName": {"text": "Old Shine"}, "userRatingCount": 90}]})}
    sent = []

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            sent.append((json.get("pageToken"), "nextPageToken" in headers["X-Goog-FieldMask"]))
            return pages[json.get("pageToken")]
    monkeypatch.setattr(dm, "http_client", lambda *a, **k: C())
    monkeypatch.setattr(dm, "_google_check", lambda r: r)
    out, units = dm.GooglePlaces(key="k").search_nosite("auto detailing", "Gastonia, NC", 60)
    assert sent == [(None, True), ("t2", True), ("t3", True)] and units == 3
    assert [(x.place_id, x.review_count, x.website) for x in out] == [("B", 2, ""), ("C", 90, "")]
    sent.clear()
    assert dm.GooglePlaces(key="k").search_nosite("x", "y", 20)[1] == 1 and len(sent) == 1   # default: one page


def test_ai_answer_matching_ignores_town_and_trade_words():
    from leadgen.strategies.missing_from_ai_answers import MissingFromAiAnswers as M
    ans = {"query": "What are the best auto detailing businesses in Gastonia, NC?", "town": "Gastonia",
           "answer": "Top auto detailing in Gastonia: **Kolby's Auto Spa** and **FC Auto Detailing**."}
    assert not M.named("Gaston Shine Detailing", ans)          # only town/trade words overlap
    assert M.named("Kolby's Auto Spa & Detailing LLC", ans)     # its own words are in the answer
    assert M.named("Gastonia Auto Detailing", ans)              # nothing of its own: no claim made


def test_deep_nosite_search_is_reused_for_a_day(settings, ledger):
    from leadgen.prospect import Prospector

    class Maps:
        calls = 0

        def __init__(self, **_):
            pass

        def search_nosite(self, query, geo, limit):
            Maps.calls += 1
            return [Candidate(name="New Shine", website="", place_id="P1", review_count=2),
                    Candidate(name="Old Shine", website="", place_id="P2", review_count=90)], 3
    e, _ = make_engine(settings, ledger)
    c = with_strategy(e, ledger, "new_business")
    p = Prospector(e)
    p.nosite().maps = Rotation("discovery", [{"name": "m"}], {"m": Maps}, e.secrets, ledger)
    cell = c["icp"]["cells"][0]
    first = p.nosite_listings(c, cell, 10)
    again = Prospector(e)
    again._nosite = p._nosite
    assert [x["place_id"] for x in first] == ["P1"] == [x["place_id"] for x in again.nosite_listings(c, cell, 10)]
    assert Maps.calls == 1
    cached = again._nosite_cache(again.strategy(c), cell)
    assert cached and set(cached[0]) == {"place_id", "name", "geo", "provider"}       # no counts kept


def test_tavily_answer_adapter_and_plan_limit(monkeypatch):
    seen = {}

    def ok(req):
        import json as _j
        seen.update(_j.loads(req.content))
        return httpx.Response(200, json={"answer": "Try **Shine Co**.", "results": [{"title": "Yelp", "url": "u"}]})
    mock_http(monkeypatch, ok)
    assert signals.TavilyAnswer(key="k").answer("best?") == ({"answer": "Try **Shine Co**.", "sources": ["Yelp"]}, 1)
    assert seen["include_answer"] and seen["search_depth"] == "basic"
    mock_http(monkeypatch, lambda req: httpx.Response(432, text="plan limit"))
    with pytest.raises(QuotaExhausted):
        signals.TavilyAnswer(key="k").answer("best?")


def test_tavily_is_the_first_answer_source():
    from leadgen.capabilities import load_registry
    by = {p["name"]: p for p in load_registry()["providers"]}
    assert by["tavily_answer"]["priority"] < by["linkup_answer"]["priority"]
    assert list(signals.SIGNALS["answer"]) == ["tavily_answer", "linkup_answer"]
