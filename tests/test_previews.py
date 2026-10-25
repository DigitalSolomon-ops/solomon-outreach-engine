"""Task 3: preview_yes -> exactly one POST (no review fields) -> poll -> ready, or held + Marcus emailed."""
from datetime import timedelta

import pytest

from leadgen.previews import PreviewClient, PreviewError, request_body, review_keys, with_utm
from leadgen.store import utcnow
from test_engine import always_in_window, reply  # noqa: F401
from test_preview_replies import _domain, _preview_thread

BASE = "https://pw.example-ds.com"


class FakeAPI:
    """Stands in for pw-site-factory. statuses: GET answers in order, the last one repeats."""

    def __init__(self, statuses=("building", "ready"), fail_post=0):
        self.posts, self.gets = [], []
        self.statuses = list(statuses)
        self.fail_post = fail_post

    def request(self, body):
        if review_keys(body):
            raise AssertionError("review fields reached the API")
        if self.fail_post:
            self.fail_post -= 1
            raise PreviewError("POST /previews: ConnectError")
        self.posts.append(body)
        return {"preview_id": "pv_" + "a" * 20, "status": "building", "industry_fallback": False}

    def status(self, pid):
        self.gets.append(pid)
        st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        ready = st == "ready"
        return {"preview_id": pid, "status": st, "expires_at": "2026-11-22T00:00:00Z",
                "preview_url": f"{BASE}/p/{pid}/" if ready else None,
                "screenshot_url": f"{BASE}/previews/{pid}/screenshot.png" if ready else None,
                "checkout_url": f"https://buy.example-ds.com/x?client_reference_id={pid}&utm_source=leadgen",
                "error": "quality gate failed" if st == "failed" else None}


def _yes(settings, ledger, api, text="Sure, show me", **kw):
    e, smtp, c, first, box = _preview_thread(settings, ledger, **kw)
    e._preview_client = api
    box["msgs"] = [reply(first["To"], first["Message-ID"], text)]
    return e, smtp, c, first, box


def _q(ledger, c, first):
    return ledger.s.get("preview_queue", f"{c['id']}__{_domain(first)}")


def test_yes_makes_exactly_one_post_without_review_fields_then_ready(settings, ledger):
    api = FakeAPI(statuses=["building", "ready"])
    e, smtp, c, first, box = _yes(settings, ledger, api)
    lead = ledger.lead(c["id"], _domain(first))
    assert lead["maps"].get("rating")                         # the lead record HAS a rating ...
    out = e.tick()
    assert out["previews"]["requested"] == 1 and len(api.posts) == 1
    body = api.posts[0]
    assert not review_keys(body) and "maps" not in body       # ... and it never leaves
    assert body["lead_id"] == f"{c['id']}__{_domain(first)}" and body["industry"] == "roofing"
    assert body["business_name"] == lead["name"] and body["current_website"] == lead["website"]
    assert body["utm"]["utm_source"] == "leadgen" and body["utm"]["utm_term"] == "preview"
    assert _q(ledger, c, first)["status"] == "requested"
    box["msgs"] = []
    e.tick()
    q = _q(ledger, c, first)
    assert q["status"] == "ready" and len(api.posts) == 1
    assert "utm_source=leadgen" in q["preview_url"] and f"utm_campaign={c['id']}" in q["preview_url"]
    assert "utm_term=preview" in q["checkout_url"] and q["screenshot_url"].endswith("screenshot.png")
    assert ledger.thread(f"{c['id']}__{_domain(first)}")["preview"]["status"] == "ready"
    assert ledger.lead(c["id"], _domain(first))["preview"]["status"] == "ready"


def test_second_yes_and_more_ticks_never_post_twice(settings, ledger):
    api = FakeAPI(statuses=["building"])
    e, smtp, c, first, box = _yes(settings, ledger, api)
    e.tick()
    box["msgs"] = [reply(first["To"], first["Message-ID"], "yes please")]
    e.tick()
    e.tick()
    assert len(api.posts) == 1 and len(api.gets) == 3


@pytest.mark.parametrize("st", ["failed", "expired"])
def test_failed_preview_holds_the_thread_and_emails_marcus(settings, ledger, st):
    api = FakeAPI(statuses=[st])
    e, smtp, c, first, box = _yes(settings, ledger, api)
    n0 = len(smtp.sent)
    out = e.tick()
    q = _q(ledger, c, first)
    assert out["previews"]["held"] == 1 and q["status"] == "held" and st in q["held_reason"]
    assert ledger.thread(f"{c['id']}__{_domain(first)}")["state"] == "escalated"
    assert any(ev["kind"] == "preview_held" and ev["notify"] for ev in ledger.events())
    assert len(smtp.sent) == n0                                 # no Email 2, no AI reply
    e.tick()
    assert len(api.posts) == 1                                  # held means held: no rebuild


def test_not_ready_after_15_minutes_is_held(settings, ledger):
    api = FakeAPI(statuses=["building"])
    e, smtp, c, first, box = _yes(settings, ledger, api)
    e.tick()
    assert _q(ledger, c, first)["status"] == "requested"
    box["msgs"] = []
    e.tick(now=utcnow() + timedelta(minutes=16))
    q = _q(ledger, c, first)
    assert q["status"] == "held" and "not ready 15 minutes" in q["held_reason"]


def test_api_down_retries_then_holds(settings, ledger):
    api = FakeAPI(statuses=["ready"], fail_post=10)
    e, smtp, c, first, box = _yes(settings, ledger, api)
    out = e.tick()
    assert out["previews"]["errors"] == 1 and _q(ledger, c, first)["status"] == "queued"
    box["msgs"] = []
    e.tick(now=utcnow() + timedelta(minutes=16))
    q = _q(ledger, c, first)
    assert q["status"] == "held" and "kept failing" in q["held_reason"] and not api.posts


def test_no_key_configured_holds_after_the_timeout(settings, ledger):
    e, smtp, c, first, box = _yes(settings, ledger, None)
    e._preview_client = None
    e.secrets.values.pop(settings["previews"]["key_secret"], None)
    e.tick()
    assert _q(ledger, c, first)["status"] == "queued"
    box["msgs"] = []
    e.tick(now=utcnow() + timedelta(minutes=16))
    assert _q(ledger, c, first)["status"] == "held"


def test_stop_tag_after_the_yes_cancels_the_preview(settings, ledger):
    tags = {}
    api = FakeAPI()
    e, smtp, c, first, box = _yes(settings, ledger, api, ghl_tags=lambda em: tags.get(em, []))
    tags[first["To"]] = ["pw-booked"]
    e.tick()
    assert _q(ledger, c, first)["status"] == "cancelled" and not api.posts


def test_suppressed_after_the_yes_cancels_the_preview(settings, ledger):
    api = FakeAPI()
    e, smtp, c, first, box = _yes(settings, ledger, api)
    ledger.suppress(first["To"], "unsubscribed", "link")
    e.tick()
    assert _q(ledger, c, first)["status"] == "cancelled" and not api.posts


# ---------------------------------------------------------------- units
def test_request_body_whitelist_and_google_lead():
    c = {"id": "roofers-1", "preview": {"enabled": True, "industry": "roofing"}}
    lead = {"domain": "acme.com", "name": "Acme Roofing", "geo": "Gastonia, NC", "website": "https://acme.com/",
            "place_id": "ChIJx", "maps": {"rating": 4.9, "review_count": 80, "hours": ["Mon: 8-5"]},
            "reviews": ["great"], "years_in_business": {"since": 2006}, "evidence": {"review:1": "x"}}
    b = request_body(c, lead)
    assert set(b) <= {"lead_id", "campaign_id", "industry", "business_name", "city", "service_area", "services",
                      "phone", "years_in_business", "hours", "current_website", "gbp_place_id", "utm"}
    assert "phone" not in b and b["gbp_place_id"] == "ChIJx" and b["hours"] == "Mon: 8-5"
    assert b["years_in_business"] == utcnow().year - 2006 and b["service_area"] == ["Gastonia"]
    assert not review_keys(b)


def test_client_refuses_review_fields_before_the_wire():
    class Never:
        def request(self, *a, **k):
            raise AssertionError("sent")
    with pytest.raises(PreviewError):
        PreviewClient(BASE, "k", http=Never()).request({"lead_id": "x", "extra": {"star_rating": 5}})


def test_client_maps_http_errors():
    class R:
        status_code = 401

        def json(self):
            return {"error": "X-Preview-Key missing or wrong"}

    class H:
        def request(self, method, url, **kw):
            assert kw["headers"]["X-Preview-Key"] == "k" and url == BASE + "/previews/pv_1"
            return R()
    with pytest.raises(PreviewError, match="401"):
        PreviewClient(BASE, "k", http=H()).status("pv_1")


def test_with_utm_keeps_existing_params():
    u = with_utm("https://buy.x/a?client_reference_id=pv_1&utm_source=outreach", {"utm_source": "leadgen", "utm_term": "preview"})
    assert "client_reference_id=pv_1" in u and "utm_source=leadgen" in u and "utm_term=preview" in u


def test_ai_reply_links_after_a_preview_carry_the_preview_id(settings, ledger):
    from conftest import FakeLLM
    api = FakeAPI(statuses=["building"])
    e, smtp, c, first, box = _yes(settings, ledger, api)
    e.tick()                                                   # preview requested: the lead has a preview_id
    pid = _q(ledger, c, first)["preview_id"]
    e.llm.client = FakeLLM(classify={"class": "interested", "confidence": 0.95, "referral_email": "", "summary": "s"})
    box["msgs"] = [reply(first["To"], first["Message-ID"], "Can we talk this week about getting started")]
    e.tick()
    ai = smtp.sent[-1][0].get_body(("plain",)).get_content()
    assert f"preview_id={pid}" in ai and "utm_source=leadgen" in ai and c["booking_url"].split("?")[0] in ai
