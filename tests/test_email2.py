"""Task 4: Email 2 is a template with slots, sent once in the same thread, behind the email2_live gate."""
import pytest

from conftest import FakeLLM
from leadgen import campaigns as camp
from leadgen.email2 import Email2Error, render
from test_engine import always_in_window, reply  # noqa: F401
from test_preview_replies import _domain, _preview_thread
from test_previews import FakeAPI


def _yes(settings, ledger, *, live, statuses=("ready",), ghl_tags=None, llm=None):
    settings["previews"]["email2_live"] = live
    e, smtp, c, first, box = _preview_thread(settings, ledger, llm=llm, ghl_tags=ghl_tags)
    api = FakeAPI(statuses=list(statuses))
    e._preview_client = api
    box["msgs"] = [reply(first["To"], first["Message-ID"], "Sure, show me")]
    return e, smtp, c, first, box, api


def _email2s(smtp):
    return [(m, env) for m, env in smtp.sent if "Get started now" in m.get_body(("html",)).get_content()]


def _q(ledger, c, first):
    return ledger.s.get("preview_queue", f"{c['id']}__{_domain(first)}")


def test_gate_off_renders_for_marcus_and_sends_nothing(settings, ledger):
    e, smtp, c, first, box, api = _yes(settings, ledger, live=False)
    e.tick()
    box["msgs"] = []
    e.tick()
    e.tick()
    assert _email2s(smtp) == []
    q = _q(ledger, c, first)
    assert q["status"] == "ready" and q["review_notified"] and "Get started" not in q["email2_draft"]
    reviews = [ev for ev in ledger.events() if ev["kind"] == "email2_review"]
    assert len(reviews) == 1 and reviews[0]["notify"]          # one notice, not one per tick
    assert "Here is the preview I built for" in reviews[0]["detail"]


def test_live_sends_one_threaded_email2_with_screenshot_and_both_buttons(settings, ledger):
    llm = FakeLLM()
    e, smtp, c, first, box, api = _yes(settings, ledger, live=True, llm=llm)
    yes = box["msgs"][0]
    calls0 = len(llm.calls)
    out = e.tick()
    assert out["previews"]["email2_sent"] == 1
    assert len(llm.calls) == calls0                              # no model touched the yes or Email 2
    [(msg, env)] = _email2s(smtp)
    assert msg["To"] == first["To"] and settings["replies"]["bcc"] in env and "Bcc" not in msg
    assert msg["Subject"] == "Re: " + first["Subject"]
    assert msg["In-Reply-To"] == yes["Message-ID"] and first["Message-ID"] in msg["References"]
    assert msg["List-Unsubscribe"] and msg["List-Unsubscribe-Post"]
    q = _q(ledger, c, first)
    htm = msg.get_body(("html",)).get_content()
    assert f'<img src="{q["screenshot_url"]}"' in htm
    assert "See your preview" in htm and "Get started now" in htm
    assert q["checkout_url"].replace("&", "&amp;") in htm and q["preview_url"].replace("&", "&amp;") in htm
    text = msg.get_body(("plain",)).get_content()
    assert c["offer"]["facts"][0] in text and "100 Example St" in text and "/u/" in text
    # Every word is the template or a stored slot: the body is exactly what render() produces.
    lead = ledger.lead(c["id"], _domain(first))
    expected = render(c, lead, q, ledger.thread(f"{c['id']}__{_domain(first)}"))
    assert text.replace("\r\n", "\n").startswith(expected["text"])
    assert q["status"] == "sent" and q["email2_message_id"] == msg["Message-ID"]
    thread = ledger.thread(f"{c['id']}__{_domain(first)}")
    assert thread["messages"][-1]["kind"] == "email2"
    assert ledger.thread_for_message(msg["Message-ID"]) == f"{c['id']}__{_domain(first)}"


def test_email2_is_sent_exactly_once(settings, ledger):
    e, smtp, c, first, box, api = _yes(settings, ledger, live=True, statuses=("building", "ready"))
    e.tick()
    box["msgs"] = []
    for _ in range(3):
        e.tick()
    assert len(_email2s(smtp)) == 1 and len(api.posts) == 1


def test_stop_tag_after_ready_cancels_email2(settings, ledger):
    tags = {}
    e, smtp, c, first, box, api = _yes(settings, ledger, live=True, statuses=("building", "ready"),
                                       ghl_tags=lambda em: tags.get(em, []))
    e.tick()                                                     # requested, still building
    tags[first["To"]] = ["presence:customer"]
    box["msgs"] = []
    e.tick()
    assert _email2s(smtp) == [] and _q(ledger, c, first)["status"] == "cancelled"


def test_missing_price_fact_holds_for_marcus(settings, ledger):
    e, smtp, c, first, box, api = _yes(settings, ledger, live=True)
    ledger.save_campaign(c["id"], {"preview": {**c["preview"], "price_fact": ""}})
    e.tick()
    q = _q(ledger, c, first)
    assert _email2s(smtp) == [] and q["status"] == "held" and "price_fact" in q["held_reason"]
    assert any(ev["kind"] == "preview_held" and ev["notify"] for ev in ledger.events())


def test_kill_switch_waits_then_sends(settings, ledger):
    e, smtp, c, first, box, api = _yes(settings, ledger, live=True)
    ledger.set_state("kill_switch", on=True)
    e.tick()
    assert _email2s(smtp) == [] and _q(ledger, c, first)["status"] == "ready"
    ledger.set_state("kill_switch", on=False)
    box["msgs"] = []
    e.tick()
    assert len(_email2s(smtp)) == 1


# ---------------------------------------------------------------- units
ITEM = {"preview_url": "https://pw.x/p/pv_1/?utm_source=leadgen&utm_term=preview",
        "screenshot_url": "https://pw.x/previews/pv_1/screenshot.png",
        "checkout_url": "https://buy.x/a?client_reference_id=pv_1&utm_source=leadgen"}
C = {"offer": {"facts": ["It is free.", "Launch is $499, then $49 a month."], "sender_name": "Marcus Barber"},
     "preview": {"price_fact": "Launch is $499, then $49 a month."}}
THREAD = {"subject": "A quick look at Acme's website"}


def test_render_slots_escaping_and_greeting():
    out = render(C, {"name": 'Bob & Sons <Roofing>', "owner": {"first_name": "Bob"}}, ITEM, THREAD)
    assert out["subject"] == "Re: A quick look at Acme's website"
    assert out["text"].startswith("Hi Bob,\n\nHere is the preview I built for Bob & Sons <Roofing>:")
    assert "Bob &amp; Sons &lt;Roofing&gt;" in out["html"] and "<Roofing>" not in out["html"]
    assert "Launch is $499, then $49 a month." in out["text"] and out["text"].endswith("Marcus")
    assert render(C, {"name": "Acme"}, ITEM, THREAD)["text"].startswith("Hi there,")


def test_render_refuses_missing_slots():
    with pytest.raises(Email2Error):
        render(C, {"name": "Acme"}, {**ITEM, "screenshot_url": None}, THREAD)
    with pytest.raises(Email2Error):
        render({**C, "preview": {"price_fact": "Launch is $399."}}, {"name": "Acme"}, ITEM, THREAD)


def test_campaign_price_fact_must_be_an_offer_fact(settings):
    c = camp.new_campaign(settings, name="Roofers", description="d", landing_url="https://x.example/r",
                          offer={"name": "Preview", "facts": ["Launch is $499."]})
    with pytest.raises(camp.CampaignError):
        camp.apply_patch(c, {"preview": {"enabled": True, "industry": "roofing"}})
    with pytest.raises(camp.CampaignError):
        camp.apply_patch(c, {"preview": {"enabled": True, "price_fact": "Launch is $399."}})
    ok = camp.apply_patch(c, {"preview": {"enabled": True, "industry": "roofing", "price_fact": "Launch is $499."}})
    assert ok["preview"]["price_fact"] == "Launch is $499."


def test_gate_switch_state_turns_email2_on_without_a_redeploy(settings, ledger):
    e, smtp, c, first, box, api = _yes(settings, ledger, live=False)
    ledger.set_state("previews", email2_live=True)       # what scripts/gate_email2.py writes on LIVE
    e.tick()
    assert len(_email2s(smtp)) == 1


def test_paid_under_another_email_stops_by_preview_id(settings, ledger):
    """The owner pays from a personal address: that contact carries the stop tag and our preview id."""
    paid = {}
    e, smtp, c, first, box, api = _yes(settings, ledger, live=True, statuses=("building", "ready"))
    e._stop_preview_lookup = lambda pid: paid.get(pid, [])
    e.tick()                                                     # POSTed, still building
    pid = _q(ledger, c, first)["preview_id"]
    paid[pid] = ["presence:customer"]                            # a different contact, same PW Preview ID
    box["msgs"] = []
    e.tick()
    assert _email2s(smtp) == [] and _q(ledger, c, first)["status"] == "cancelled"
    assert ledger.is_suppressed(email=first["To"])


def test_a_stripe_test_checkout_link_is_never_sent(settings, ledger):
    class TestLinkAPI(FakeAPI):
        def status(self, pid):
            v = super().status(pid)
            v["checkout_url"] = f"https://buy.stripe.com/test_abc?client_reference_id={pid}"
            return v
    settings["previews"]["email2_live"] = True
    e, smtp, c, first, box = _preview_thread(settings, ledger)
    e._preview_client = TestLinkAPI(statuses=["ready"])
    box["msgs"] = [reply(first["To"], first["Message-ID"], "Sure, show me")]
    e.tick()
    q = _q(ledger, c, first)
    assert _email2s(smtp) == [] and q["status"] == "held" and "test link" in q["held_reason"]
