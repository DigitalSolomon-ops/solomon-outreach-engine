"""2026-10-24 Outreach controls: fixed industry list, clone, who-to-target guard, preflight now."""
import pytest
from fastapi.testclient import TestClient

from leadgen import campaigns as camp
from leadgen import industries as inds
from leadgen.api import create_app
from leadgen.store import utcnow
from test_engine import always_in_window, make_engine, seed_active  # noqa: F401

PRICE = "The website is $600 to build, then $97 a month."


def client_for(e):
    return TestClient(create_app(lambda: e))


def website_launch(e, ledger, slug="roofing", status="draft"):
    ind = inds.get(slug)
    c = camp.new_campaign(e.settings, name=f"Website Launch · {ind['label']} · Gaston/Charlotte",
                          description=f"Website Launch for {ind['label'].lower()} around Gaston County.",
                          landing_url=f"https://digitalsolomon.com/presence/websites/{slug}",
                          booking_url=f"https://api.digitalsolomon.com/widget/booking/B1T?pw_industry={slug}",
                          offer={"name": "Website Launch", "facts": ["I build a free preview.", ind["h1"], PRICE],
                                 "faq": [{"q": "q", "a": "a"}]},
                          cells=inds.cells_for(ind))
    c.update(status=status, preview={"enabled": True, "industry": slug, "price_fact": PRICE})
    c["icp"]["site_filter"] = "weak_or_none"
    camp.validate(c)
    ledger.save_campaign(c["id"], c)
    return ledger.campaign(c["id"])


# ---------------------------------------------------------------- industry list
def test_catalog_has_the_ten_funnel_industries():
    lst = inds.public_list()
    slugs = [i["slug"] for i in lst["industries"]]
    assert len(slugs) == 10 and len(set(slugs)) == 10 and "roofing" in slugs and "med-spa" in slugs
    assert lst["default_geos"][0] == "Gastonia, NC" and len(lst["default_geos"]) == 6
    with pytest.raises(inds.IndustryError):
        inds.get("dentist")                                  # fixed list: no custom industries


def test_api_lists_industries_and_create_uses_one(settings, ledger):
    e, _ = make_engine(settings, ledger)
    cl = client_for(e)
    assert {i["slug"] for i in cl.get("/industries").json()["industries"]} >= {"plumbing", "hvac"}
    r = cl.post("/campaigns", json={"name": "Audit · Plumbers", "description": "d", "offer_name": "Audit",
                                    "landing_url": "https://digitalsolomon.com/audit", "industry": "plumbing"})
    c = r.json()
    assert r.status_code == 200 and c["industry"] == "plumbing"
    assert {x["segment"] for x in c["icp"]["cells"]} == {"Plumbers"} and len(c["icp"]["cells"]) == 6
    assert all(x["query"] == "plumber" for x in c["icp"]["cells"])
    bad = cl.post("/campaigns", json={"name": "x", "description": "d", "landing_url": "https://x.example/",
                                      "industry": "dentist"})
    assert bad.status_code == 400 and "unknown industry" in bad.json()["detail"]


# ---------------------------------------------------------------- clone
def test_clone_website_launch_swaps_everything_industry_specific(settings, ledger):
    e, _ = make_engine(settings, ledger)
    src = website_launch(e, ledger, "roofing", status="paused")
    src_settings = {**src["settings"], "daily_cap": 12}
    ledger.save_campaign(src["id"], {"settings": src_settings})
    r = client_for(e).post(f"/campaigns/{src['id']}/clone", json={"industry": "plumbing"})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["name"] == "Website Launch · Plumbers · Gaston/Charlotte" and c["status"] == "draft"
    assert c["industry"] == "plumbing" and c["preview"]["industry"] == "plumbing" and c["preview"]["enabled"]
    assert c["landing_url"].endswith("/websites/plumbing") and c["booking_url"].endswith("pw_industry=plumbing")
    assert inds.get("plumbing")["h1"] in c["offer"]["facts"] and inds.get("roofing")["h1"] not in c["offer"]["facts"]
    assert PRICE in c["offer"]["facts"] and c["preview"]["price_fact"] == PRICE
    assert c["offer"]["faq"][0]["q"] == inds.get("plumbing")["faq"][0]["q"]
    assert {x["segment"] for x in c["icp"]["cells"]} == {"Plumbers"}
    assert [x["geo"] for x in c["icp"]["cells"]] == [x["geo"] for x in src["icp"]["cells"]]
    assert c["icp"]["site_filter"] == "weak_or_none" and c["settings"]["daily_cap"] == 12
    assert c["cloned_from"] == src["id"] and "plumbers" in c["description"]
    assert ledger.campaign(src["id"])["status"] == "paused"                       # source untouched


def test_clone_refuses_same_industry_duplicates_and_unknown(settings, ledger):
    e, _ = make_engine(settings, ledger)
    src = website_launch(e, ledger, "roofing")
    cl = client_for(e)
    assert cl.post(f"/campaigns/{src['id']}/clone", json={"industry": "roofing"}).status_code == 400
    assert cl.post(f"/campaigns/{src['id']}/clone", json={"industry": "hvac"}).status_code == 200
    assert cl.post(f"/campaigns/{src['id']}/clone", json={"industry": "hvac"}).status_code == 409
    assert cl.post(f"/campaigns/{src['id']}/clone", json={"industry": "dentist"}).status_code == 400


def test_clone_of_a_non_preview_campaign_keeps_its_offer_and_pages(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = seed_active(e, ledger)
    ledger.save_campaign(c["id"], {"status": "draft"})
    r = client_for(e).post(f"/campaigns/{c['id']}/clone", json={"industry": "hvac"})
    out = r.json()
    assert r.status_code == 200, r.text
    assert out["landing_url"] == c["landing_url"] and out["offer"]["facts"] == c["offer"]["facts"]
    assert not out["preview"].get("enabled") and {x["segment"] for x in out["icp"]["cells"]} == {"HVAC companies"}


# ---------------------------------------------------------------- who to target
def test_who_to_target_changes_only_while_draft_or_paused(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = website_launch(e, ledger, status="waiting")
    cl = client_for(e)
    r = cl.patch(f"/campaigns/{c['id']}", json={"icp": {"site_filter": "weak"}})
    assert r.status_code == 409 and "Pause it first" in r.json()["detail"]
    assert cl.patch(f"/campaigns/{c['id']}", json={"settings": {"daily_cap": 9}}).status_code == 200   # other edits fine
    ledger.save_campaign(c["id"], {"status": "paused"})
    r = cl.patch(f"/campaigns/{c['id']}", json={"icp": {"site_filter": "weak"}})
    assert r.status_code == 200 and r.json()["icp"]["site_filter"] == "weak"
    assert any(ev["kind"] == "site_filter" for ev in ledger.events())


# ---------------------------------------------------------------- preflight: pass bar, now, progress
def test_pass_bar_is_validated_and_used(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = camp.ensure_seed(ledger, settings)
    assert c["settings"]["samples_required"] == 5
    for bad in (0, 6, "3", True):
        with pytest.raises(camp.CampaignError):
            camp.apply_patch(c, {"settings": {"samples_required": bad}})
    ledger.save_campaign(c["id"], {"settings": {**c["settings"], "samples_required": 3}})
    calls = []
    from leadgen import prospect as pr_mod
    real = pr_mod.Prospector.samples

    def three_of_five(self, c, n=3, max_queries=None, progress=None):
        calls.append(n)
        out = real(self, c, n=n, max_queries=max_queries, progress=progress)
        for i, d in enumerate(out):
            d["passed"] = i < 3
        return out
    pr_mod.Prospector.samples = three_of_five
    try:
        pf = e.preflight(ledger.campaign(c["id"]))
    finally:
        pr_mod.Prospector.samples = real
    assert calls == [5] and pf["checks"]["samples"]["ok"] and pf["samples_required"] == 3
    assert "needs 3" in pf["checks"]["samples"]["detail"]


def test_preflight_now_starts_the_job_and_tick_skips_it(settings, ledger):
    started = []
    e, _ = make_engine(settings, ledger)
    e._job_runner = lambda args: started.append(args)
    c = camp.ensure_seed(ledger, settings)
    cl = client_for(e)
    assert cl.post(f"/campaigns/{c['id']}/preflight").status_code == 400            # draft: not launched
    e.launch(c["id"], run_preflight=False)
    r = cl.post(f"/campaigns/{c['id']}/preflight")
    assert r.status_code == 200 and started == [["preflight", c["id"]]]
    assert r.json()["preflight_progress"]["state"] == "queued"
    assert cl.post(f"/campaigns/{c['id']}/preflight").status_code == 409            # already in flight
    e.tick()
    assert ledger.campaign(c["id"])["preflight"] is None                          # tick left it to the job
    out = e.run_preflight(c["id"])                                               # what the job runs
    after = ledger.campaign(c["id"])
    assert out["status"] == "testing" and after["preflight"]["ok"]
    assert after["preflight_progress"]["state"] == "done" and after["preflight_progress"]["step"] == "Passed"


def test_preflight_reports_progress_while_it_runs(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = camp.ensure_seed(ledger, settings)
    seen = []
    real = e._progress
    e._progress = lambda cid, state, **kw: (seen.append((state, kw.get("step"), kw.get("done"), kw.get("total"))),
                                            real(cid, state, **kw))
    e.launch(c["id"])
    steps = [s for s in seen if s[0] == "running"]
    assert steps[0][1].startswith("Checking")
    assert any(s[1] == "Writing sample emails" and s[3] == 5 for s in steps)
    assert ledger.campaign(c["id"])["preflight_progress"]["state"] == "done"


def test_stale_in_flight_marker_does_not_block_forever(settings, ledger):
    e, _ = make_engine(settings, ledger)
    from datetime import timedelta
    old = (utcnow() - timedelta(minutes=45)).isoformat()
    assert not e.preflight_in_flight({"preflight_progress": {"state": "running", "at": old}})
    assert e.preflight_in_flight({"preflight_progress": {"state": "running", "at": utcnow().isoformat()}})
    assert not e.preflight_in_flight({"preflight_progress": {"state": "done", "at": utcnow().isoformat()}})


def test_detail_counts_weak_site_leads(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = website_launch(e, ledger, status="active")
    ledger.save_lead(c["id"], "a.com", {"status": "in_sequence", "site_quality": {"weak": True, "reasons": ["not_mobile"]}})
    ledger.save_lead(c["id"], "b.com", {"status": "excluded", "reason": "site is not weak",
                                        "site_quality": {"weak": False, "reasons": []}})
    lo = client_for(e).get(f"/campaigns/{c['id']}/detail").json()["lead_outcomes"]
    assert lo["weak_site_leads"] == 1 and lo["exclusion_reasons"] == {"site is not weak": 1}


def test_launching_a_paused_campaign_clears_the_pause_reason(settings, ledger):
    """Roofers 2026-10-24: paused, then launched again, kept "paused by the operator" and the page
    showed a Paused banner on a waiting campaign."""
    e, _ = make_engine(settings, ledger)
    c = camp.ensure_seed(ledger, settings)
    cl = client_for(e)
    e.launch(c["id"], run_preflight=False)
    assert cl.post(f"/campaigns/{c['id']}/pause").status_code == 200
    assert ledger.campaign(c["id"])["paused_reason"]
    e.launch(c["id"], run_preflight=False)
    after = ledger.campaign(c["id"])
    assert after["status"] == "waiting" and not after.get("paused_reason")
