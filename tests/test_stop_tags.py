"""Stop tags: presence:booked / presence:customer on the GHL contact stop every send, every campaign."""
import pytest

import leadgen.sender as sender_mod

from conftest import FakeSecrets
from leadgen.stoptags import StopTags, StopTagUnavailable, ghl_lookup
from leadgen.store import utcnow
from test_engine import _sent_thread, always_in_window, make_engine, reply, seed_active  # noqa: F401


@pytest.mark.parametrize("tag", ["presence:booked", "presence:customer", "pw-booked", "pw-paid", "pw-launched",
                                 "pw-no-show", "pw-no-purchase"])
def test_tagged_contact_gets_no_first_touch(settings, ledger, tag):
    tags = {}
    e, smtp = make_engine(settings, ledger, ghl_tags=lambda email: tags.get(email, []))
    c = seed_active(e, ledger, daily_cap=3)
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    tagged = leads[0]["email"]
    tags[tagged] = ["leadgen", tag]
    out = e.tick()
    to = [m["To"] for m, _ in smtp.sent]
    assert tagged not in to and len(to) == len(leads) - 1 and out["send"]["sent"] == len(leads) - 1
    assert ledger.is_suppressed(email=tagged)
    assert ledger.lead(c["id"], leads[0]["domain"])["status"] == "suppressed"
    assert any(ev["kind"] == "stop_tag" for ev in ledger.events())


@pytest.mark.parametrize("tag", ["presence:booked", "presence:customer"])
def test_tag_added_after_first_touch_stops_the_follow_up(settings, ledger, tag):
    tags = {}
    e, smtp = make_engine(settings, ledger, ghl_tags=lambda email: tags.get(email, []))
    c = seed_active(e, ledger, daily_cap=2)
    e._first_touch_target = lambda c: 1
    e.prospect()
    e.tick()
    [lead] = ledger.leads(c["id"], "in_sequence")
    ledger.save_lead(c["id"], lead["domain"], {"seq": {**lead["seq"], "next_due": sender_mod.today_et().isoformat()}})
    e.prospect()
    assert "1" in ledger.lead(c["id"], lead["domain"])["seq"]["drafts"]
    n = len(smtp.sent)
    tags[lead["email"]] = [tag]
    e.tick()
    assert len(smtp.sent) == n
    assert ledger.lead(c["id"], lead["domain"])["status"] == "suppressed"


def test_tagged_in_one_campaign_is_stopped_in_every_campaign(settings, ledger):
    from leadgen import campaigns as camp
    tags = {}
    e, smtp = make_engine(settings, ledger, ghl_tags=lambda email: tags.get(email, []))
    c = seed_active(e, ledger, daily_cap=1)
    e.prospect()
    [lead] = ledger.leads(c["id"], "in_sequence")
    other = camp.new_campaign(settings, name="Other", description="d", landing_url="https://x.example/o",
                              offer={"name": "Other", "facts": ["It is free."]})
    other["status"] = "active"
    ledger.save_campaign(other["id"], other)
    ledger.save_lead(other["id"], lead["domain"], {**lead, "status": "in_sequence"})
    tags[lead["email"]] = ["presence:customer"]
    e.tick()
    assert smtp.sent == []


def test_ghl_unreadable_holds_the_send_without_suppressing(settings, ledger):
    def down(email):
        raise StopTagUnavailable("GHL search HTTP 503")
    e, smtp = make_engine(settings, ledger, ghl_tags=down)
    c = seed_active(e, ledger, daily_cap=2)
    e.prospect()
    out = e.tick()
    assert smtp.sent == [] and out["send"]["sent"] == 0
    assert any("stop-tag check unavailable" in why for why in out["send"]["skipped"].values())
    assert all(not ledger.is_suppressed(email=l["email"]) for l in ledger.leads(c["id"]))


def test_no_ghl_credentials_holds_every_send(settings, ledger):
    e, smtp = make_engine(settings, ledger)
    e._stop_lookup = None                     # no stop_lookup and no global-ghl-leadgen secret
    seed_active(e, ledger, daily_cap=2)
    e.prospect()
    out = e.tick()
    assert smtp.sent == [] and out["send"]["sent"] == 0


def test_tagged_contact_reply_goes_to_marcus_not_the_ai(settings, ledger):
    tags = {}
    e, smtp, c, first, box = _sent_thread(settings, ledger, ghl_tags=lambda email: tags.get(email, []))
    tags[first["To"]] = ["presence:booked"]
    n0 = len(smtp.sent)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "I'm interested, how does this work?")]
    out = e.tick()
    assert len(smtp.sent) == n0 and out["replies"]["escalated"] == 1


# ---------------------------------------------------------------- the GHL read itself
class Resp:
    def __init__(self, code, data):
        self.status_code, self._data = code, data

    def json(self):
        return self._data


class Client:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


CREDS = {"token": "pit", "location_id": "loc1"}


def test_ghl_lookup_reads_tags_with_the_email_in_the_body_not_the_url():
    client = Client(Resp(200, {"contacts": [{"email": "Bob@Roof.com", "tags": ["presence:customer"]},
                                             {"email": "other@roof.com", "tags": ["presence:booked"]}]}))
    tags = ghl_lookup(CREDS, client)("bob@roof.com")
    assert tags == ["presence:customer"]
    url, kw = client.calls[0]
    assert "bob@roof.com" not in url and kw["json"]["filters"][0]["value"] == "bob@roof.com"
    assert kw["json"]["locationId"] == "loc1"


def test_ghl_lookup_error_is_unavailable_not_clear():
    with pytest.raises(StopTagUnavailable):
        ghl_lookup(CREDS, Client(Resp(500, {})))("bob@roof.com")


def test_stop_tags_cache_and_suppress(settings, ledger):
    calls = []
    st = StopTags(settings, ledger, lambda e: calls.append(e) or ["Presence:Booked"])
    assert st.check("Bob@Roof.com") == "Presence:Booked" and st.check("bob@roof.com")
    assert calls == ["bob@roof.com"] and ledger.is_suppressed(email="bob@roof.com")
    assert StopTags(settings, ledger, lambda e: ["leadgen"]).check("x@y.com") is None


def test_engine_reads_the_ghl_secret_for_stop_tags(settings, ledger):
    e, _ = make_engine(settings, ledger)
    e._stop_lookup = None
    cfg = settings["stop_tags"]
    e.secrets = FakeSecrets({cfg["token_secret"]: "pit", cfg["location_secret"]: "loc1"})
    assert e.stop_tags().lookup is not None
    e.secrets = FakeSecrets({cfg["token_secret"]: "pit"})
    assert e.stop_tags().lookup is None


def test_ghl_preview_lookup_filters_on_the_pw_preview_id_field():
    from leadgen.stoptags import ghl_preview_lookup
    client = Client(Resp(200, {"contacts": [{"email": "owner.personal@gmail.com", "tags": ["presence:customer"]}]}))
    tags = ghl_preview_lookup(CREDS, "Vn5VviGo11MDC4eGzIl1", client)("pv_" + "a" * 20)
    assert tags == ["presence:customer"]
    f = client.calls[0][1]["json"]["filters"][0]
    assert f == {"field": "customFields.Vn5VviGo11MDC4eGzIl1", "operator": "eq", "value": "pv_" + "a" * 20}


def test_preview_id_hit_suppresses_the_lead_email(settings, ledger):
    st = StopTags(settings, ledger, lambda e: [], lambda pid: ["presence:booked"] if pid == "pv_x" else [])
    assert st.check("lead@roof.com") is None
    assert st.check("lead@roof.com", "pv_x") == "presence:booked" and ledger.is_suppressed(email="lead@roof.com")


def test_preview_id_without_a_preview_lookup_holds(settings, ledger):
    with pytest.raises(StopTagUnavailable):
        StopTags(settings, ledger, lambda e: []).check("lead@roof.com", "pv_x")
