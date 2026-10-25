"""Lead sources (decision 1): weak-site filter for the domain flow, and the no-website pilot."""
from datetime import date

import pytest

from conftest import FakeMaps, FakeScrape
from leadgen import campaigns as camp
from leadgen.pilot_nosite import NoSitePilot, business_email, names_match, profile_url, report, to_markdown
from leadgen.providers import Candidate, Page, ProviderError
from leadgen.rotation import Rotation
from leadgen.sitequality import assess
from test_engine import always_in_window, make_engine, seed_active  # noqa: F401

TODAY = date(2026, 10, 23)
MODERN = ('<html><head><meta name="viewport" content="width=device-width"><script src="/a.js"></script></head>'
          '<body><h1>Acme Roofing</h1><p>Roof replacement and repair across Gastonia.</p>'
          + "<p>Family owned crew with free estimates and written warranties on every job.</p>" * 3
          + "<footer>© 2026 Acme Roofing</footer></body></html>")


def page(html, raw=True):
    return Page(url="https://acmeroofing.com/", html=html, raw_html=raw)


# ---------------------------------------------------------------- site quality
def test_modern_site_is_not_weak():
    assert assess([page(MODERN)], today=TODAY) == {"weak": False, "reasons": [], "facts": []}


def test_no_viewport_old_copyright_flash_and_old_markup_are_weak():
    html = (MODERN.replace('<meta name="viewport" content="width=device-width">', "")
            .replace("© 2026", "© 2015") + '<embed src="intro.swf"><font>a</font><font>b</font><center>c</center>')
    q = assess([page(html)], today=TODAY)
    assert q["weak"] and set(q["reasons"]) == {"not_mobile", "old_copyright", "flash", "old_markup"}
    assert any("2015" in f["text"] for f in q["facts"]) and all(f["id"].startswith("weak_") for f in q["facts"])


def test_broken_and_parked_are_weak():
    assert assess([], fetch_failed=True, today=TODAY)["reasons"] == ["broken"]
    parked = page('<html><head><meta name="viewport"></head><body>This domain is for sale. Buy this domain.</body></html>')
    assert "parked" in assess([parked], today=TODAY)["reasons"]


def test_text_reader_alone_never_concludes_not_mobile():
    text_only = Page(url="https://x.com/", markdown="Acme Roofing " * 40 + " © 2026", raw_html=False)
    assert assess([text_only], today=TODAY)["weak"] is False


def test_site_filter_is_validated(settings):
    c = camp.new_campaign(settings, name="Roofers", description="d", landing_url="https://x.example/r",
                          offer={"name": "Preview", "facts": ["It is free."]})
    assert c["icp"]["site_filter"] == "any" and c["preview"]["enabled"] is False
    with pytest.raises(camp.CampaignError):
        camp.apply_patch(c, {"icp": {"site_filter": "pretty"}})
    assert camp.apply_patch(c, {"icp": {"site_filter": "weak"}})["icp"]["site_filter"] == "weak"


class ModernScrape(FakeScrape):
    def fetch(self, url):
        p, n = super().fetch(url)
        p.html = p.html.replace("<head>", '<head><meta name="viewport" content="width=device-width">') + "© 2026"
        return p, n


def _weak_campaign(e, ledger):
    c = seed_active(e, ledger, daily_cap=3)
    ledger.save_campaign(c["id"], {"icp": {**c["icp"], "site_filter": "weak"}})
    return ledger.campaign(c["id"])


def test_weak_filter_keeps_weak_sites_with_the_reasons_as_facts(settings, ledger):
    e, smtp = make_engine(settings, ledger)          # FakeScrape pages have no viewport tag
    c = _weak_campaign(e, ledger)
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads and all("not_mobile" in l["site_quality"]["reasons"] for l in leads)
    assert all(any(f["id"] == "weak_not_mobile" for f in l["facts"]) for l in leads)


def test_weak_filter_drops_good_sites(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": ModernScrape}, e.secrets, ledger)
    e.reader = e.scrape
    c = _weak_campaign(e, ledger)
    res = e.prospect()
    assert res["campaigns"][c["id"]]["first_touches"]["site_not_weak"] > 0
    assert not ledger.leads(c["id"], "in_sequence")
    assert all(l["reason"] == "site is not weak" for l in ledger.leads(c["id"], "excluded"))


def test_any_filter_is_unchanged_for_good_sites(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": ModernScrape}, e.secrets, ledger)
    c = seed_active(e, ledger, daily_cap=2)
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert len(leads) == 2 and all("site_quality" not in l for l in leads)


class DeadScrape:
    def __init__(self, **_):
        pass

    def fetch(self, url):
        raise ProviderError("connection refused")


def test_broken_site_under_weak_filter_needs_another_email_source(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": DeadScrape}, e.secrets, ledger)
    e.reader = e.scrape
    c = _weak_campaign(e, ledger)
    e.prospect()
    rows = ledger.leads(c["id"], "no_email")
    assert rows and all(r["site_quality"]["reasons"] == ["broken"] for r in rows)   # never guessed


# ---------------------------------------------------------------- maps adapters, no-site mode
class Resp:
    status_code = 200

    def __init__(self, data):
        self._d = data
        self.text = str(data)

    def json(self):
        return self._d


def test_google_places_nosite_returns_only_listings_without_a_site(monkeypatch):
    from leadgen.providers import discovery_more as dm
    data = {"places": [{"id": "ChIJ1", "displayName": {"text": "No Site Roofing"}},
                       {"id": "ChIJ2", "displayName": {"text": "Has Site Roofing"}, "websiteUri": "https://h.com"}]}

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return Resp(data)
    monkeypatch.setattr(dm, "http_client", lambda *a, **k: C())
    monkeypatch.setattr(dm, "_google_check", lambda r: r.json())
    g = dm.GooglePlaces(key="k")
    nosite, _ = g.search_nosite("roofing", "Gastonia, NC", 20)
    assert [(x.place_id, x.website) for x in nosite] == [("ChIJ1", "")]
    normal, _ = g.search("roofing", "Gastonia, NC", 20)
    assert [x.place_id for x in normal] == ["ChIJ2"]


# ---------------------------------------------------------------- pilot helpers
def test_profile_urls_and_name_match():
    assert profile_url("facebook", "https://www.facebook.com/AcmeRoofingNC/")
    assert not profile_url("facebook", "https://www.facebook.com/groups/roofers")
    assert profile_url("yelp", "https://www.yelp.com/biz/acme-roofing-gastonia")
    assert not profile_url("yelp", "https://www.yelp.com/search?find_desc=roofing")
    assert profile_url("bbb", "https://www.bbb.org/us/nc/gastonia/profile/roofing/acme-roofing-0473-1")
    assert not profile_url("bbb", "https://acme.com/bbb")
    assert names_match("Acme Roofing LLC", "ACME roofing and gutters") and not names_match("Acme Roofing", "Zenith Roof Co")


def test_business_email_ignores_platform_addresses_and_unnamed_pages():
    p = Page(url="https://facebook.com/acme", html="Acme Roofing. Email acmeroofs@gmail.com or notification@facebookmail.com")
    assert business_email(p, "Acme Roofing") == "acmeroofs@gmail.com"
    other = Page(url="https://facebook.com/x", html="Zenith Roofing. zenith@gmail.com")
    assert business_email(other, "Acme Roofing") is None


# ---------------------------------------------------------------- the pilot end to end
class NoSiteMaps(FakeMaps):
    def search_nosite(self, query, geo, limit):
        return [Candidate(name=f"Roofer {i} Gastonia", website="", place_id=f"ChIJ{i}", source="fake")
                for i in range(limit)], 1


class Web:
    kind = "web"

    def __init__(self, **_):
        pass

    def search(self, query, geo, limit):
        name = query.rsplit(" ", 1)[0]
        n = int(name.split()[1])
        slug = name.lower().replace(" ", "-")
        if query.endswith("facebook") and n % 2 == 0:
            return [Candidate(name=f"{name} | Facebook", website=f"https://www.facebook.com/{slug}")], 1
        if query.endswith("bbb") and n % 3 == 0:
            return [Candidate(name=f"{name} | BBB", website=f"https://www.bbb.org/us/nc/gastonia/profile/roofing/{slug}-1")], 1
        return [], 1


class ProfileScrape:
    def __init__(self, **_):
        pass

    def fetch(self, url):
        slug = url.rstrip("/").split("/")[-1].removesuffix("-1")
        name = slug.replace("-", " ").title()
        n = int(slug.split("-")[1])
        email = f"{slug.replace('-', '')}@gmail.com" if n % 4 == 0 or "bbb.org" in url else ""
        return Page(url=url, html=f"<html><body><h1>{name}</h1><p>{email}</p></body></html>"), 1


def test_pilot_runs_on_n_listings_keyed_by_listing_id_and_reports(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    e.discover = Rotation("discovery", [{"name": "m", "kind": "maps", "cap": 500}, {"name": "w", "kind": "web"}],
                          {"m": NoSiteMaps, "w": Web}, e.secrets, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": ProfileScrape}, e.secrets, ledger)
    e.reader = e.scrape
    rep = NoSitePilot(e, ["facebook", "yelp", "bbb"]).run("roofing contractor", ["Gastonia, NC"], n=12)
    assert rep["listings"] == 12
    docs = {d: x for d, x in ledger.s.all("pilot_nosite")}
    assert set(docs) == {f"gbp_ChIJ{i}" for i in range(12)}
    # FB found for even n; email printed only when n % 4 == 0; BBB found (with email) for n % 3 == 0
    found = {k for k, v in docs.items() if v["status"] == "found"}
    assert found == {f"gbp_ChIJ{i}" for i in range(12) if i % 4 == 0 or i % 3 == 0}
    assert docs["gbp_ChIJ4"]["email_source"].startswith("facebook:")
    assert docs["gbp_ChIJ3"]["email_source"].startswith("bbb:")
    assert all(v["email"] is None for k, v in docs.items() if k not in found)          # dropped, not guessed
    assert all("rating" not in v and "reviews" not in v for v in docs.values())
    assert rep["emails_found"] == len(found) and rep["find_rate"] == round(len(found) / 12, 3)
    assert rep["by_source"]["facebook"]["profile_found"] == 6
    assert "| Email found on an own profile page |" in to_markdown(rep)
    assert not [l for _, l in ledger.s.all("leads")]          # pilot leads never enter a campaign
    again = NoSitePilot(e, ["facebook", "yelp", "bbb"]).run("roofing contractor", ["Gastonia, NC"], n=12)
    assert again["listings"] == 8                              # listings from the first run are not redone
    assert len(ledger.s.all("pilot_nosite")) == 20


def test_pilot_defaults_to_bbb_only(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    e.discover = Rotation("discovery", [{"name": "m", "kind": "maps", "cap": 500}, {"name": "w", "kind": "web"}],
                          {"m": NoSiteMaps, "w": Web}, e.secrets, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": ProfileScrape}, e.secrets, ledger)
    e.reader = e.scrape
    rep = NoSitePilot(e).run("roofing contractor", ["Gastonia, NC"], n=12)
    assert list(rep["by_source"]) == ["bbb"]
    docs = [d for _, d in ledger.s.all("pilot_nosite")]
    assert all(set(d["profiles"]) == {"bbb"} for d in docs)
    assert rep["emails_found"] == 4                          # n % 3 == 0: 0, 3, 6, 9


def test_report_handles_no_listings():
    assert report([], query="q", geos=["g"], target=50)["find_rate"] == 0.0


# ---------------------------------------------------------------- 2026-10-24: Website Launch lead sources
from datetime import timedelta  # noqa: E402

from leadgen.prospect import Prospector, nosite_key  # noqa: E402
from leadgen.store import utcnow  # noqa: E402
from leadgen.writer import validate as validate_draft  # noqa: E402

PRICE = "The website is $600 to build, then $97 a month."


def _launch_campaign(e, ledger, site_filter="weak_or_none"):
    c = seed_active(e, ledger, daily_cap=20)
    offer = {**c["offer"], "name": "Website Launch", "facts": [*c["offer"]["facts"], PRICE]}
    ledger.save_campaign(c["id"], {"offer": offer, "preview": {"enabled": True, "industry": "roofing", "price_fact": PRICE},
                                   "icp": {**c["icp"], "site_filter": site_filter, "cells": c["icp"]["cells"][:1]}})
    return ledger.campaign(c["id"])


def _nosite_engine(settings, ledger, scrape=None):
    """Maps returns modern sites (domain flow finds nothing weak) plus no-website listings."""
    class Maps(NoSiteMaps):
        pass
    e, smtp = make_engine(settings, ledger)
    e.discover = Rotation("discovery", [{"name": "m", "kind": "maps", "cap": 500}, {"name": "w", "kind": "web"}],
                          {"m": Maps, "w": Web}, e.secrets, ledger)

    class Mixed:
        def __init__(self, **_):
            self.modern, self.profile = ModernScrape(), ProfileScrape()

        def fetch(self, url):
            return (self.profile if "bbb.org" in url or "facebook.com" in url else self.modern).fetch(url)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": scrape or Mixed}, e.secrets, ledger)
    e.reader = e.scrape
    return e, smtp


def test_http_only_site_is_weak_but_https_echo_is_not():
    http_page = Page(url="http://acmeroofing.com/", html=MODERN, raw_html=True)
    assert assess([http_page], today=TODAY)["reasons"] == ["not_secure"]
    assert assess([page(MODERN)], today=TODAY)["weak"] is False
    text_only = Page(url="http://acmeroofing.com/", markdown="Acme Roofing " * 40, raw_html=False)
    assert "not_secure" not in assess([text_only], today=TODAY)["reasons"]       # needs a raw fetch


def test_weak_or_none_is_website_launch_only(settings):
    c = camp.new_campaign(settings, name="Audit", description="d", landing_url="https://x.example/a",
                          offer={"name": "Audit", "facts": ["It is free.", PRICE]})
    with pytest.raises(camp.CampaignError, match="Website Launch"):
        camp.apply_patch(c, {"icp": {"site_filter": "weak_or_none"}})
    ok = camp.apply_patch(c, {"preview": {"enabled": True, "industry": "roofing", "price_fact": PRICE},
                              "icp": {"site_filter": "weak_or_none"}})
    assert ok["icp"]["site_filter"] == "weak_or_none"


def test_text_only_read_is_parked_for_recheck_not_excluded(settings, ledger):
    class TextOnly:
        def __init__(self, **_):
            pass

        def fetch(self, url):
            return Page(url=url, markdown="Acme Roofing serves Gastonia. " * 30 + "info@acme.com", raw_html=False), 1
    e, smtp = make_engine(settings, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": TextOnly}, e.secrets, ledger)
    e.reader = e.scrape
    c = _weak_campaign(e, ledger)
    res = e.prospect()
    rows = ledger.leads(c["id"], "site_unassessed")
    assert rows and not ledger.leads(c["id"], "excluded") and not ledger.leads(c["id"], "in_sequence")
    assert res["campaigns"][c["id"]]["first_touches"]["site_unassessed"] == len(rows)
    pr = Prospector(e)
    assert pr.known(c, rows[0]["domain"])                                       # not before the retry date
    ledger.save_lead(c["id"], rows[0]["domain"], {"retry_after": (utcnow() - timedelta(days=1)).isoformat()})
    assert not pr.known(c, rows[0]["domain"])                                   # re-checked after it


def test_weak_or_none_adds_no_website_leads_keyed_by_listing_id(settings, ledger):
    e, smtp = _nosite_engine(settings, ledger)
    c = _launch_campaign(e, ledger)
    res = e.prospect()
    st = res["campaigns"][c["id"]]["first_touches"]
    assert st["site_not_weak"] > 0 and st["nosite_listings"] > 0
    leads = [l for l in ledger.leads(c["id"], "in_sequence") if l.get("nosite")]
    assert leads, st
    for l in leads:
        assert l["domain"] == nosite_key(l["place_id"]) and l["website"] == ""
        assert l["email_source"].startswith("bbb:") and l["email"].endswith("@gmail.com")
        assert "fact:nosite" in l["evidence"] and "rating" not in l and not l.get("reviews")
    assert all(not l.get("email") for l in ledger.leads(c["id"], "no_email") if l.get("nosite"))   # never guessed
    # a second run does not redo listings already on file
    before = len([l for l in ledger.leads(c["id"]) if l.get("nosite")])
    e.prospect()
    keys = [l["domain"] for l in ledger.leads(c["id"]) if l.get("nosite")]
    assert len(keys) == len(set(keys)) and len(keys) >= before


@pytest.mark.parametrize("site_filter", ["weak", "any"])
def test_other_filters_never_touch_no_website_listings(settings, ledger, site_filter):
    e, smtp = _nosite_engine(settings, ledger)
    c = _launch_campaign(e, ledger, site_filter=site_filter)
    e.prospect()
    assert not [l for l in ledger.leads(c["id"]) if l.get("nosite")]


def test_preflight_samples_can_come_from_no_website_listings(settings, ledger):
    e, smtp = _nosite_engine(settings, ledger)
    c = _launch_campaign(e, ledger)
    drafts = Prospector(e).samples(c, n=3)
    assert len(drafts) == 3 and all(d["domain"].startswith("gbp-") for d in drafts)


def test_validator_blocks_saw_your_website_for_no_website_leads(settings):
    c = camp.new_campaign(settings, name="WL", description="d", landing_url="https://x.example/r",
                          offer={"name": "Website Launch", "facts": [PRICE]})
    lead = {"name": "Acme Roofing", "domain": "gbp-ChIJ1", "nosite": True}
    bad = {"subject": "A new website for Acme Roofing",
           "body": "Hi Acme Roofing,\n\nI spent a minute on your website and noticed a few things. " * 3 + "\n\nMarcus"}
    good = {"subject": "A new website for Acme Roofing",
            "body": "Hi Acme Roofing,\n\nWhen I searched for you online I couldn't find a website for the business. " * 3
                    + "\n\nMarcus"}
    kw = dict(max_words=200, min_words=10)
    assert any("no website" in p for p in validate_draft(bad, c, lead, {"fact:nosite": "x"}, 0, **kw))
    assert not any("no website" in p for p in validate_draft(good, c, lead, {"fact:nosite": "x"}, 0, **kw))
    assert not any("no website" in p for p in validate_draft(bad, c, {**lead, "nosite": False}, {}, 0, **kw))


def test_detail_reports_why_leads_did_not_go_forward(settings, ledger):
    from fastapi.testclient import TestClient
    from leadgen.api import create_app
    e, smtp = _nosite_engine(settings, ledger)
    c = _launch_campaign(e, ledger)
    e.prospect()
    out = TestClient(create_app(lambda: e)).get(f"/campaigns/{c['id']}/detail").json()["lead_outcomes"]
    assert out["exclusion_reasons"].get("site is not weak", 0) > 0
    assert out["site_checks"].get("none_found", 0) > 0 and out["no_website_leads"] > 0


# --------------------------------------------------------------------------- new-business trigger
class ReviewedNoSiteMaps(NoSiteMaps):
    """No-website listings whose review counts climb by 3: 0, 3, 6, 9, ... (None for the first, as
    Google omits the count when a listing has no reviews)."""
    def search_nosite(self, query, geo, limit):
        return [Candidate(name=f"Roofer {i} Gastonia", website="", place_id=f"ChIJ{i}", source="fake",
                          review_count=None if i == 0 else i * 3) for i in range(limit)], 1


def test_new_listing_is_website_launch_only_and_bounds_max_reviews(settings):
    c = camp.new_campaign(settings, name="Audit", description="d", landing_url="https://x.example/a",
                          offer={"name": "Audit", "facts": ["It is free.", PRICE]})
    with pytest.raises(camp.CampaignError, match="Website Launch"):
        camp.apply_patch(c, {"icp": {"site_filter": "new_listing"}})
    pv = {"enabled": True, "industry": "auto-detailing", "price_fact": PRICE}
    with pytest.raises(camp.CampaignError, match="max_reviews"):
        camp.apply_patch(c, {"preview": pv, "icp": {"site_filter": "new_listing", "max_reviews": 500}})
    ok = camp.apply_patch(c, {"preview": pv, "icp": {"site_filter": "new_listing", "max_reviews": 3}})
    assert ok["icp"]["site_filter"] == "new_listing" and ok["icp"]["max_reviews"] == 3


def test_new_listing_skips_websites_and_keeps_only_listings_with_few_reviews(settings, ledger):
    e, smtp = _nosite_engine(settings, ledger)
    e.discover = Rotation("discovery", [{"name": "m", "kind": "maps", "cap": 500}, {"name": "w", "kind": "web"}],
                          {"m": ReviewedNoSiteMaps, "w": Web}, e.secrets, ledger)
    c = _launch_campaign(e, ledger, site_filter="new_listing")
    res = e.prospect()
    st = res["campaigns"][c["id"]]["first_touches"]
    assert st["candidates"] == 0 and st["site_not_weak"] == 0          # no website pass at all
    rows = [l for l in ledger.leads(c["id"]) if l.get("nosite")]
    assert rows
    # max_reviews defaults to 5: only listings 0 (no count), 1 (3 reviews) qualify
    assert {l["place_id"] for l in rows} <= {"ChIJ0", "ChIJ1"}
    assert all("review_count" not in l and "rating" not in l for l in rows)   # Google counts never stored


def test_new_listing_preflight_samples_never_use_websites(settings, ledger):
    e, smtp = _nosite_engine(settings, ledger)
    c = _launch_campaign(e, ledger, site_filter="new_listing")
    ledger.save_campaign(c["id"], {"icp": {**c["icp"], "max_reviews": 50}})
    c = ledger.campaign(c["id"])
    drafts = Prospector(e).samples(c, n=2)
    assert drafts and all(d["domain"].startswith("gbp-") for d in drafts)
