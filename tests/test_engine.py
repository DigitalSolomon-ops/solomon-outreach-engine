"""End to end with fakes: prospect -> tick sends -> replies -> stops, plus the launch state machine."""
from datetime import timedelta
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

import leadgen.sender as sender_mod
from conftest import MAILBOX, MODELS, FakeLLM, FakeMaps, FakeScrape, FakeSecrets, FakeSMTP, FakeVerify
from leadgen import campaigns as camp
from leadgen.compliance import sign_token
from leadgen.engine import Engine
from leadgen.providers import Candidate
from leadgen.rotation import Rotation
from leadgen.store import send_id, utcnow
from leadgen.unsubscribe import create_app

SECRETS = {"global-leadgen-hmac": "hmackey", "global-leadgen-mailbox-1": MAILBOX, "global-anthropic": "k"}


class GooglePlacesFake(FakeMaps):
    persist = "place_id_only"

    def search(self, query, geo, limit):
        cands, n = super().search(query, geo, limit)
        for c in cands:
            c.place_id = "ChIJ" + c.domain[:8]
            c.reviews = ["Great service, fast response."]
        return cands, n


class Http:
    """Landing page + unsubscribe health stand-in."""
    def __init__(self, landing_ok=True):
        self.landing_ok = landing_ok

    def get(self, url, **kw):
        class R:
            pass
        r = R()
        if url.endswith("/health"):
            r.status_code, r.text = 200, '{"ok": true}'
            r.json = lambda: {"ok": True}
        else:
            r.status_code = 200 if self.landing_ok else 404
            r.text = "Take the free AI Readiness Audit" if self.landing_ok else "not found"
        return r


def make_engine(settings, ledger, *, llm=None, smtp=None, imap=None, maps=FakeMaps, landing_ok=True, verify=FakeVerify,
                ghl_tags=None):
    """ghl_tags: {email: [tags]} for the stop-tag check; a callable replaces the lookup outright."""
    secrets = FakeSecrets(SECRETS)
    smtp = smtp or FakeSMTP()
    tags = {} if ghl_tags is None else ghl_tags
    lookup = tags if callable(tags) else (lambda email: tags.get(email, []))
    e = Engine(settings, ledger, secrets, models=MODELS, llm_client=llm or FakeLLM(), smtp_factory=smtp,
               imap_factory=imap or (lambda mb: []), secret_names=lambda: set(SECRETS), http=Http(landing_ok),
               stop_lookup=lookup)
    e.discover = Rotation("discovery", [{"name": "m", "kind": "maps", "cap": 500}], {"m": maps}, secrets, ledger)
    e.scrape = Rotation("scrape", [{"name": "s"}], {"s": FakeScrape}, secrets, ledger)
    e.reader = e.scrape
    e.verify = Rotation("verify", [{"name": "v"}], {"v": verify}, secrets, ledger)
    e.people = None
    e.sizing = None
    e.notices = FakeSMTP()                 # notices to Marcus are kept apart from prospect mail
    e.notifier.smtp_factory = e.notices
    ledger.s.set("mailboxes", "1", {"id": "1", "status": "active", "health": {"ok": True}})
    return e, smtp


@pytest.fixture(autouse=True)
def always_in_window(monkeypatch):
    monkeypatch.setattr(sender_mod, "in_window", lambda cfg, now=None: (True, ""))
    monkeypatch.setattr(sender_mod, "ticks_left_today", lambda cfg, now=None: 1)


def seed_active(e, ledger, **settings_over):
    c = camp.ensure_seed(ledger, e.settings)
    c["icp"]["cells"] = c["icp"]["cells"][:3]
    ledger.save_campaign(c["id"], {"status": "active", "icp": c["icp"],
                                   "settings": {**c["settings"], **settings_over}})
    return ledger.campaign(c["id"])


def reply(frm, to_mid, body, subject="Re: A quick look", **hdrs):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = MAILBOX["email"]
    m["Subject"] = subject
    m["Message-ID"] = f"<r{abs(hash(body)) % 10**8}@acme>"
    m["In-Reply-To"] = to_mid
    for k, v in hdrs.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m


# ---------------------------------------------------------------- prospect + tick
def test_seed_starts_as_draft_and_nothing_sends(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    e.prospect()
    out = e.tick()
    assert ledger.campaign(camp.SEED_ID)["status"] == "draft"
    assert out["send"]["status"] == "nothing due" and smtp.sent == []


def test_prospect_then_tick_sends_first_touch_with_compliance(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    c = seed_active(e, ledger, daily_cap=3)
    res = e.prospect()
    assert res["campaigns"][c["id"]]["first_touches"]["first_touches"] == 3
    lead = ledger.lead(c["id"], "hvacgastonia.com")
    assert lead["email"] == "info@hvacgastonia.com" and lead["email_source"].startswith("site:")
    assert lead["brief"]["strongest_observation"]["evidence"] in lead["evidence"]
    assert ledger.lead(c["id"], "yelp.com") is None
    out = e.tick()
    assert out["send"]["sent"] == 3 and len(smtp.sent) == 3
    msg = smtp.sent[0][0]
    text = msg.get_body(("plain",)).get_content()
    assert "https://unsub.example-ds.com/u/" in text and "100 Example St" in text
    assert "utm_term=step0" in text and f"utm_campaign={c['id']}" in text
    rec = ledger.send(send_id(c["id"], "hvacgastonia.com", 0))
    assert rec["status"] == "sent"
    assert ledger.lead(c["id"], "hvacgastonia.com")["seq"]["next_step"] == 1
    costs = ledger.costs_since(utcnow() - timedelta(minutes=5))
    assert costs and all(r["usd"] > 0 for r in costs)


def test_kill_switch_checked_before_every_send(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    c = seed_active(e, ledger, daily_cap=3)
    e.prospect()
    orig = smtp.__call__

    class Flip(FakeSMTP):
        pass
    calls = {"n": 0}

    def flipping(creds):
        conn = orig(creds)
        real = conn.send

        def send(msg, to_addrs=None):
            calls["n"] += 1
            ledger.set_state("kill_switch", on=True)       # switched on after the first send
            return real(msg, to_addrs)
        conn.send = send
        return conn
    e.smtp_factory = flipping
    out = e.tick()
    assert out["send"]["sent"] == 1 and "kill switch" in out["send"]["status"]
    assert c["id"]


def test_exactly_once_stale_sending_is_never_resent(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    c = seed_active(e, ledger, daily_cap=1)
    e.prospect()
    [lead] = ledger.leads(c["id"], "in_sequence")
    sid = send_id(c["id"], lead["domain"], 0)
    # A previous tick claimed it and died before recording the outcome.
    assert ledger.begin_send(sid, {"campaign_id": c["id"], "domain": lead["domain"], "step": 0})
    assert not ledger.begin_send(sid, {})                   # a second claim is refused
    ledger.s.set("sends", sid, {"claimed_at": (utcnow() - timedelta(minutes=11)).isoformat()})
    out = e.tick()
    assert out["expired_sending"] == 1 and ledger.send(sid)["status"] == "unknown"
    assert smtp.sent == []                                  # missing one beats sending twice
    e.tick()
    assert smtp.sent == []


def test_smtp_failure_mid_send_leaves_claim_unknown(settings, ledger):
    import smtplib
    e, smtp = make_engine(settings, ledger)
    c = seed_active(e, ledger, daily_cap=1)
    e.prospect()
    smtp.fail_next = smtplib.SMTPServerDisconnected("gone")
    e.tick()
    [lead] = ledger.leads(c["id"], "in_sequence")
    sid = send_id(c["id"], lead["domain"], 0)
    assert ledger.send(sid)["status"] == "sending"
    ledger.s.set("sends", sid, {"claimed_at": (utcnow() - timedelta(minutes=11)).isoformat()})
    e.tick()
    assert ledger.send(sid)["status"] == "unknown" and smtp.sent == []


def test_lease_lock_blocks_overlapping_ticks(settings, ledger):
    e, _ = make_engine(settings, ledger)
    assert ledger.s.lease("tick", "other-holder", timedelta(minutes=15))
    assert e.tick()["status"].startswith("skipped")
    ledger.s.set("locks", "tick", {"holder": "other-holder", "until": (utcnow() - timedelta(seconds=1)).isoformat()})
    assert "run_id" in e.tick()                             # an expired lease is taken over


def test_followup_threads_under_first_email(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    c = seed_active(e, ledger, daily_cap=2)
    ledger.s.set("campaigns", c["id"], {"settings": {**c["settings"], "daily_cap": 2}})
    e._first_touch_target = lambda c: 1
    e.prospect()
    e.tick()
    [lead] = ledger.leads(c["id"], "in_sequence")
    first = smtp.sent[0][0]
    ledger.save_lead(c["id"], lead["domain"], {"seq": {**lead["seq"], "next_due": sender_mod.today_et().isoformat()}})
    e.prospect()
    lead = ledger.lead(c["id"], lead["domain"])
    assert "1" in lead["seq"]["drafts"]
    ledger.s.set("sends", "pad", {})  # no-op
    e.tick()
    second = smtp.sent[-1][0]
    assert second["Subject"] == first["Subject"] and second["In-Reply-To"] == first["Message-ID"]
    assert first["Message-ID"] in second["References"]
    assert "advertisement" in second.get_body(("plain",)).get_content()


def test_google_places_persists_only_place_id(settings, ledger):
    e, _ = make_engine(settings, ledger, maps=GooglePlacesFake)
    e.discover = Rotation("discovery", [{"name": "google_places", "kind": "maps", "cap": 500}],
                          {"google_places": GooglePlacesFake}, e.secrets, ledger)
    c = seed_active(e, ledger, daily_cap=1)
    e.prospect()
    [lead] = ledger.leads(c["id"])
    assert lead["place_id"].startswith("ChIJ")
    assert lead["maps"] == {} and lead.get("reviews") == [] and "phone" not in lead
    assert not any(k.startswith(("maps:", "review:")) for k in lead["evidence"])
    raw = str(ledger.s.data["leads"])
    assert "Great service" not in raw and "4.6" not in raw


def test_mx_only_capped_at_20_percent_of_day(settings, ledger):
    class NoVerifiers:
        def __init__(self, **_): pass
        def verify(self, email):
            from leadgen.providers import QuotaExhausted
            raise QuotaExhausted("402 out of credits")
    e, smtp = make_engine(settings, ledger, verify=NoVerifiers)
    import leadgen.providers.verify as v
    v.MxCheck.verify = lambda self, email: ("mx_only", 0)
    c = seed_active(e, ledger, daily_cap=10)
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads and all(l["verdict"] == "mx_only" for l in leads)
    out = e.tick()
    # Mailbox day-0 warmup is 5: 20% of 5 = 1 mx_only send.
    assert out["send"]["sent"] == 1


# ---------------------------------------------------------------- replies
def _sent_thread(settings, ledger, llm=None, inbox=None, ghl_tags=None):
    box = {"msgs": []}
    e, smtp = make_engine(settings, ledger, llm=llm, imap=lambda mb: box["msgs"], ghl_tags=ghl_tags)
    c = seed_active(e, ledger, daily_cap=1)
    e.prospect()
    e.tick()
    msg = smtp.sent[0][0]
    return e, smtp, c, msg, box


def test_interested_reply_gets_ai_reply_with_booking_bcc_and_stops_sequence(settings, ledger):
    e, smtp, c, first, box = _sent_thread(settings, ledger)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "I'm interested, how does this work?")]
    out = e.tick()
    assert out["replies"]["interested"] == 1 and out["replies"]["ai_replies"] == 1
    ai, env = smtp.sent[-1]
    assert "operator@example.com" in env and "Bcc" not in ai
    assert c["booking_url"] in ai.get_body(("plain",)).get_content()
    assert ai["In-Reply-To"] == box["msgs"][0]["Message-ID"] and ai["Subject"].startswith("Re: ")
    lead = ledger.lead(c["id"], first["To"].split("@")[1])
    assert lead["status"] == "replied" and lead["seq"]["next_step"] is None
    assert any(ev["kind"] == "interested_reply" and ev["notify"] for ev in ledger.events())


@pytest.mark.parametrize("klass,conf,expect_reply,expect_escalate", [
    ("question", 0.9, True, False), ("not_now", 0.9, True, False), ("referral", 0.9, True, False),
    ("pricing", 0.9, False, True), ("legal", 0.95, False, True), ("negative", 0.9, False, True),
    ("unclear", 0.9, False, True), ("interested", 0.5, False, True),
])
def test_each_reply_class_action(settings, ledger, klass, conf, expect_reply, expect_escalate):
    llm = FakeLLM(classify={"class": klass, "confidence": conf, "referral_email": "", "summary": "s"})
    e, smtp, c, first, box = _sent_thread(settings, ledger, llm=llm)
    n0 = len(smtp.sent)
    box["msgs"] = [reply(first["To"], first["Message-ID"], f"reply text for {klass}")]
    e.tick()
    assert (len(smtp.sent) > n0) == expect_reply
    lead = ledger.lead(c["id"], first["To"].split("@")[1])
    assert lead["seq"]["next_step"] is None                  # any reply stops the sequence
    esc = [ev for ev in ledger.events() if ev["kind"] == "reply_escalated"]
    assert bool(esc) == expect_escalate
    if klass == "not_now":
        assert lead["status"] == "not_now" and lead["not_now_until"] > utcnow().date().isoformat()


def test_four_ai_reply_cap_hands_thread_to_marcus(settings, ledger):
    e, smtp, c, first, box = _sent_thread(settings, ledger)
    for i in range(5):
        box["msgs"] = [reply(first["To"], first["Message-ID"], f"follow up question number {'abcde'[i]}")]
        e.tick()
    thread = ledger.thread(f"{c['id']}__{first['To'].split('@')[1]}")
    assert thread["ai_replies"] == 4 and thread["state"] == "escalated"
    assert sum(1 for ev in ledger.events() if ev["kind"] == "reply_escalated") == 1
    ai_msgs = [m for m, env in smtp.sent if m["Subject"].startswith("Re: ")]
    assert len(ai_msgs) == 4


def test_ooo_pauses_sequence_and_does_not_reply(settings, ledger):
    e, smtp, c, first, box = _sent_thread(settings, ledger)
    n0 = len(smtp.sent)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "I am out of the office until Monday.",
                         subject="Automatic reply: A quick look", Auto_Submitted="auto-replied")]
    e.tick()
    lead = ledger.lead(c["id"], first["To"].split("@")[1])
    assert len(smtp.sent) == n0 and lead["seq"]["next_step"] == 1 and lead["seq"]["paused_until"]


def test_referral_same_domain_only(settings, ledger):
    dom = "hvacgastonia.com"
    llm = FakeLLM(classify={"class": "referral", "confidence": 0.9, "referral_email": f"owner@{dom}", "summary": "s"})
    e, smtp, c, first, box = _sent_thread(settings, ledger, llm=llm)
    box["msgs"] = [reply(first["To"], first["Message-ID"], f"Talk to owner@{dom} instead")]
    e.tick()
    ref = ledger.lead(c["id"], f"{dom}~ref")
    assert ref["status"] == "referral_queued" and ref["referred_by"] == first["To"]


def test_ai_disclosure_when_asked_if_human(settings, ledger):
    disclosure = settings["replies"]["ai_disclosure"]
    llm = FakeLLM(classify={"class": "question", "confidence": 0.9, "referral_email": "", "summary": "s"},
                  reply={"body": f"Good question. {disclosure} The audit is free.\n\nMarcus", "answerable": True})
    e, smtp, c, first, box = _sent_thread(settings, ledger, llm=llm)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "Are you a real person or a bot?")]
    e.tick()
    assert disclosure in smtp.sent[-1][0].get_body(("plain",)).get_content()


def test_optout_reply_suppresses_across_campaigns(settings, ledger):
    e, smtp, c, first, box = _sent_thread(settings, ledger)
    addr = first["To"]
    other = camp.new_campaign(settings, name="Other", description="d", landing_url="https://x.example/o",
                              offer={"name": "Other", "facts": ["It is free."]})
    ledger.save_campaign(other["id"], other)
    ledger.save_lead(other["id"], addr.split("@")[1], {"email": addr, "status": "in_sequence", "seq": {"next_step": 0}})
    ledger.index_email(addr, other["id"], addr.split("@")[1])
    box["msgs"] = [reply(addr, first["Message-ID"], "Please remove me from your list.")]
    e.tick()
    assert ledger.is_suppressed(email=addr)
    assert ledger.lead(other["id"], addr.split("@")[1])["status"] == "suppressed"
    n = len(smtp.sent)
    ledger.save_campaign(other["id"], {"status": "active"})
    e.tick()
    assert len(smtp.sent) == n


def test_unsubscribe_link_suppresses_globally(settings, ledger):
    e, smtp, c, first, box = _sent_thread(settings, ledger)
    client = TestClient(create_app(ledger, "hmackey"))
    tok = sign_token(first["To"], "hmackey")
    assert client.get(f"/u/{tok}").status_code == 200
    assert client.post(f"/u/{tok}").status_code == 200
    assert ledger.is_suppressed(email=first["To"])
    assert client.post("/u/forged.token").status_code == 400
    assert client.get("/health").json() == {"ok": True}


# ---------------------------------------------------------------- launch state machine
def test_preflight_then_testing_then_active(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    c = camp.ensure_seed(ledger, settings)
    c["icp"]["cells"] = c["icp"]["cells"][:6]
    ledger.save_campaign(c["id"], {"icp": c["icp"], "settings": {**c["settings"], "test_batch_size": 2}})
    out = e.launch(c["id"])
    assert out["preflight"]["ok"], out["preflight"]
    assert out["status"] == "testing"
    e.prospect()
    e.tick()
    assert len(smtp.sent) == 2
    e.tick()
    assert len(smtp.sent) == 2                               # batch only until evaluated
    for _, s in ledger.s.where("sends", ("campaign_id", "==", c["id"])):
        ledger.s.set("sends", s["id"], {"sent_at": (utcnow() - timedelta(hours=49)).isoformat()})
    e.health()
    assert ledger.campaign(c["id"])["status"] == "active"


def test_test_batch_fails_on_bounces(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    c = camp.ensure_seed(ledger, settings)
    ledger.save_campaign(c["id"], {"settings": {**c["settings"], "test_batch_size": 2}})
    e.launch(c["id"])
    e.prospect()
    e.tick()
    for _, s in ledger.s.where("sends", ("campaign_id", "==", c["id"])):
        ledger.s.set("sends", s["id"], {"bounced": True, "sent_at": (utcnow() - timedelta(hours=49)).isoformat()})
    e.health()
    assert ledger.campaign(c["id"])["status"] == "paused"
    assert any(ev["kind"] == "campaign_paused" and ev["notify"] for ev in ledger.events())


def test_preflight_stays_waiting_without_landing_page(settings, ledger):
    e, smtp = make_engine(settings, ledger, landing_ok=False)
    c = camp.ensure_seed(ledger, settings)
    out = e.launch(c["id"])
    assert out["status"] == "waiting" and not out["preflight"]["checks"]["landing"]["ok"]
    e.prospect()
    e.tick()
    assert smtp.sent == []


def test_deferred_launch_returns_at_once_and_tick_runs_preflight(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = camp.ensure_seed(ledger, settings)
    ledger.save_campaign(c["id"], {"preflight": {"at": utcnow().isoformat(), "ok": False, "checks": {}}})  # stale, recent
    out = e.launch(c["id"], run_preflight=False)
    assert out == {"status": "waiting", "preflight": None, "preflight_deferred": True}
    assert ledger.campaign(c["id"])["preflight"] is None        # cleared, so no hourly retry gap
    e.tick()
    after = ledger.campaign(c["id"])
    assert after["preflight"] and after["preflight"]["ok"], after["preflight"]
    assert after["status"] == "testing"


def test_api_launch_does_not_wait_for_preflight(settings, ledger, monkeypatch):
    from leadgen.api import create_app as api_app
    e, _ = make_engine(settings, ledger)
    c = camp.ensure_seed(ledger, settings)
    def boom(*a, **k):
        raise AssertionError("preflight ran inside the request")
    monkeypatch.setattr(e, "preflight", boom)
    client = TestClient(api_app(lambda: e))
    r = client.post(f"/campaigns/{c['id']}/launch")
    assert r.status_code == 200 and r.json()["status"] == "waiting"


def test_campaign_auto_pause_on_bounce_rate(settings, ledger):
    e, _ = make_engine(settings, ledger)
    c = seed_active(e, ledger)
    for i in range(25):
        ledger.s.set("sends", f"x{i}", {"campaign_id": c["id"], "status": "sent", "bounced": i < 2,
                                        "sent_at": utcnow().isoformat(), "mailbox": "9"})
    assert e.kill_rules() == [c["id"]]
    assert ledger.campaign(c["id"])["status"] == "paused"


# ---------------------------------------------------------------- API
def test_engine_api_basics(settings, ledger):
    from leadgen.api import create_app as api_app
    e, _ = make_engine(settings, ledger)
    client = TestClient(api_app(lambda: e))
    assert client.get("/healthz").json()["ok"]
    camps = client.get("/campaigns").json()["campaigns"]
    assert [x["status"] for x in camps if x["id"] == camp.SEED_ID] == ["draft"]
    s = client.post(f"/campaigns/{camp.SEED_ID}/samples", json={"n": 3}).json()
    assert len(s["drafts"]) == 3 and all(d["passed"] for d in s["drafts"]) and s["cost_records"]
    assert client.get("/summary").json()["activeCampaigns"] == 0
    r = client.get("/readiness").json()
    assert {i["id"] for i in r["items"]} >= {"sending_domain", "mailbox_secret", "landing", "launch"}
    assert client.put("/settings", json={"paid_providers_monthly_cap_usd": 5}).status_code == 400
    assert client.post("/kill-switch", json={"on": True}).json() == {"kill_switch": True}
    created = client.post("/campaigns", json={"name": "Dentists", "description": "dentists in Concord",
                                              "landing_url": "https://digitalsolomon.com/audit"})
    assert created.status_code == 200 and created.json()["status"] == "draft"
    assert client.post("/campaigns", json={"name": "No landing"}).status_code == 400


def test_plan_endpoint_validates_and_computes_numbers(settings, ledger):
    from leadgen.api import create_app as api_app
    plan = {"icp_cells": [{"segment": "HVAC", "geo": "Gastonia, NC", "query": "HVAC company", "naics": "238220"}],
            "offer": {"name": "AI Readiness Audit", "facts": ["It is free."], "faq": []},
            "source_plan": {"discover_maps": ["serpapi_maps"]},
            "sequence": [{"step": 0, "day": 0, "angle": "a"}, {"step": 1, "day": 3, "angle": "b"},
                         {"step": 2, "day": 7, "angle": "c"}, {"step": 3, "day": 14, "angle": "d"}],
            "risks": ["r"], "recommended_connections": [{"name": "zerobounce", "why": "verify more"}]}
    bad = {**plan, "source_plan": {"discover_maps": ["made_up_provider"]}}
    answers = iter([bad, plan])
    e, _ = make_engine(settings, ledger, llm=FakeLLM(plan=lambda m, s, u: next(answers)))
    client = TestClient(api_app(lambda: e))
    client.get("/campaigns")
    p = client.post(f"/campaigns/{camp.SEED_ID}/plan").json()
    assert p["probe"]["queries"] <= 3 and p["probe"]["site_reads"] <= 10
    assert p["recommended_connections"][0]["vault_slot"] == "global/zerobounce"
    assert p["est_monthly_provider_cost_usd"] == 0.0 and p["days_to_full_volume"] is not None
    assert ledger.campaign(camp.SEED_ID)["plan"]["at"]


def test_candidate_without_name_is_handled(settings, ledger):
    # Discovery can return odd rows; a nameless one falls back to the domain.
    from leadgen.prospect import clean_name
    assert clean_name("", "acme.com") == "acme.com"
    assert Candidate(name="", website="https://acme.com").domain == "acme.com"


# ---------------------------------------------------------------- readiness (fix brief 2026-10-23)
def _health_state(ledger, mailbox_ok=True):
    """What a health run writes: the inventory (names only) and the mailbox health doc."""
    from leadgen.capabilities import inventory, load_registry
    ledger.set_state("capabilities", **inventory(load_registry(), {"global-leadgen-mailbox-1", "global-anthropic"}))
    ledger.s.set("mailboxes", "1", {"id": "1", "email": MAILBOX["email"], "domain": "send.example-ds.com",
                                    "status": "active", "health": {"ok": mailbox_ok, "spf": True, "dkim": True,
                                                                   "dmarc": True, "dbl": "clean",
                                                                   "checked_at": utcnow().isoformat()}})


def test_readiness_ticks_mailbox_items_from_the_health_result(settings, ledger):
    e, _ = make_engine(settings, ledger)
    ledger.s.data.pop("mailboxes", None)
    e.inv = {"at": "earlier", "providers": [], "mailbox_secrets": [], "recommended": []}
    camp.ensure_seed(ledger, settings)
    r = {i["id"]: i for i in e.readiness()["items"]}
    assert not r["sending_domain"]["done"] and not r["mailbox_secret"]["done"]
    _health_state(ledger)
    r = {i["id"]: i for i in e.readiness()["items"]}
    assert r["sending_domain"]["done"] and "1 mailbox(es) pass" in r["sending_domain"]["detail"]
    assert "checked" in r["sending_domain"]["detail"]
    assert r["mailbox_secret"]["done"] and r["mailbox_secret"]["detail"] == "global-leadgen-mailbox-1"


def test_api_readiness_is_not_stuck_on_its_startup_inventory(settings, ledger):
    """The bug: the API process kept the inventory from before the mailbox was added."""
    from leadgen.api import create_app as api_app
    e, _ = make_engine(settings, ledger)
    ledger.s.data.pop("mailboxes", None)
    e.inv = {"at": "2026-10-23T11:02:16", "providers": [], "mailbox_secrets": [], "recommended": []}
    client = TestClient(api_app(lambda: e))
    before = {i["id"]: i for i in client.get("/readiness").json()["items"]}
    assert not before["mailbox_secret"]["done"]
    _health_state(ledger)                                   # a later health run finds the mailbox
    after = {i["id"]: i for i in client.get("/readiness").json()["items"]}
    assert after["mailbox_secret"]["done"] and after["sending_domain"]["done"]
    assert e.inv["mailbox_secrets"] == ["global-leadgen-mailbox-1"]   # the engine adopted it too


@pytest.mark.parametrize("landing_ok,status_shown,ticked", [(True, "HTTP 200", True), (False, "HTTP 404", False)])
def test_health_checks_draft_landing_and_readiness_shows_it(settings, ledger, landing_ok, status_shown, ticked):
    e, _ = make_engine(settings, ledger, landing_ok=landing_ok)
    camp.ensure_seed(ledger, settings)                      # the seed is a draft
    out = e.health()
    assert out["landing"][camp.SEED_ID]["status"] == (200 if landing_ok else 404)
    assert out["landing"][camp.SEED_ID]["url"] == "https://digitalsolomon.com/audit"
    item = {i["id"]: i for i in e.readiness()["items"]}["landing"]
    assert item["done"] is ticked and status_shown in item["detail"]


def test_readiness_2xx_but_preflight_still_needs_the_offer_mentioned(settings, ledger):
    class NoMention(Http):
        def get(self, url, **kw):
            r = super().get(url, **kw)
            if not url.endswith("/health"):
                r.text = "Welcome"
            return r
    e, _ = make_engine(settings, ledger)
    e.http = NoMention()
    c = camp.ensure_seed(ledger, settings)
    land = e.check_landing(c)
    assert land["ok"] and not land["preflight_ok"]
    assert {i["id"]: i for i in e.readiness()["items"]}["landing"]["done"]
    assert not e.preflight(ledger.campaign(c["id"]))["checks"]["landing"]["ok"]


# ---------------------------------------------------------------- loopback
def test_loopback_pending_without_mailbox_secrets(settings, ledger, monkeypatch):
    import leadgen.loopback as lb
    e, smtp = make_engine(settings, ledger)
    e._secret_names = lambda: {"global-leadgen-mailbox-1"}     # no notify secret
    res = lb.run(e)
    assert res["status"] == "pending" and "mailbox secret not present" in res["detail"]
    assert smtp.sent == [] and not [c for c in ledger.campaigns() if c["id"].startswith("loopback")]


def test_loopback_end_to_end_with_fakes(settings, ledger, monkeypatch):
    import leadgen.loopback as lb
    monkeypatch.setattr(lb, "in_window", lambda cfg, now=None: (True, ""))
    notify = {**MAILBOX, "email": "operator@example.com", "user": "operator@example.com"}
    SECRETS2 = {**SECRETS, "global-leadgen-notify-smtp": notify}
    smtp = FakeSMTP()
    # The fake inbox of mailbox-1: whatever was sent TO it (the reply from the notify mailbox).
    inbox = lambda mb: [m for m, _ in smtp.sent if m["To"] == MAILBOX["email"]]
    e, _ = make_engine(settings, ledger, smtp=smtp, imap=inbox)
    e.secrets = FakeSecrets(SECRETS2)
    e._secret_names = lambda: set(SECRETS2)
    e.notifier.secrets = e.secrets
    e.health = lambda now=None: ledger.s.set("mailboxes", "1", {"id": "1", "status": "active", "health": {"ok": True}})
    res = lb.run(e, wait_seconds=5, poll_every=0, sleep=lambda s: None)
    assert res["status"] == "pass", res["steps"]
    sent_to = [m["To"] for m, _ in smtp.sent]
    assert sent_to.count("operator@example.com") >= 2   # first touch + AI reply, both to Marcus
    assert all(t in ("operator@example.com", MAILBOX["email"]) for t in sent_to)
    assert ledger.campaign(res["campaign_id"])["status"] == "done"
    assert (ledger.campaign(camp.SEED_ID) or {}).get("status") in (None, "draft")   # seed untouched
