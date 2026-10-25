"""preview_yes: a yes to "Want to see the preview I built for you?" queues one preview request."""
import pytest

from conftest import FakeLLM
from leadgen.replies import preview_yes_rule
from test_engine import _sent_thread, always_in_window, reply  # noqa: F401 - fixture is autouse


@pytest.mark.parametrize("text", ["yes", "Yes!", "Sure, show me", "send it", "Yeah send it over",
                                  "ok please", "Yes please\n\nJohn\nABC Roofing LLC", "Sure"])
def test_bare_yes_is_decided_in_code(text):
    assert preview_yes_rule(text)


@pytest.mark.parametrize("text", ["yes, how much?", "Sure, what does it cost", "not yet", "no thanks",
                                  "Is this a real site or a mockup?", "yes but I already have a web guy",
                                  "Yes\n\nbut honestly we are booked solid until spring and do not need more work"])
def test_anything_more_than_a_yes_goes_to_the_classifier(text):
    assert not preview_yes_rule(text)


def _preview_thread(settings, ledger, llm=None, enabled=True, ghl_tags=None):
    e, smtp, c, first, box = _sent_thread(settings, ledger, llm=llm, ghl_tags=ghl_tags)
    ledger.save_campaign(c["id"], {"preview": {"enabled": enabled, "industry": "roofing",
                                               "price_fact": c["offer"]["facts"][0]}})
    return e, smtp, ledger.campaign(c["id"]), first, box


def _domain(first):
    return first["To"].split("@")[1]


def test_positive_yes_queues_one_preview_and_sends_nothing(settings, ledger):
    llm = FakeLLM()
    e, smtp, c, first, box = _preview_thread(settings, ledger, llm=llm)
    n0, calls0 = len(smtp.sent), len(llm.calls)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "Sure, show me")]
    out = e.tick()
    assert out["replies"]["preview_yes"] == 1 and out["replies"]["ai_replies"] == 0
    assert not [r for r, _ in llm.calls[calls0:] if r == "classify"]       # decided in code
    assert len(smtp.sent) == n0                                           # Email 2 is not model-written
    lead = ledger.lead(c["id"], _domain(first))
    assert lead["status"] == "preview_requested" and lead["seq"]["next_step"] is None
    thread = ledger.thread(f"{c['id']}__{_domain(first)}")
    assert thread["preview"]["status"] == "queued"
    assert thread["preview"]["in_reply_to"] == box["msgs"][0]["Message-ID"]
    assert any(ev["kind"] == "preview_yes" and ev["notify"] for ev in ledger.events())


def test_classifier_yes_with_a_sentence_also_queues(settings, ledger):
    llm = FakeLLM(classify={"class": "preview_yes", "confidence": 0.9, "referral_email": "", "summary": "s"})
    e, smtp, c, first, box = _preview_thread(settings, ledger, llm=llm)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "Yes, I would like to see what you put together for us.")]
    e.tick()
    assert ledger.thread(f"{c['id']}__{_domain(first)}")["preview"]["status"] == "queued"


def test_a_second_yes_never_queues_a_second_request(settings, ledger):
    e, smtp, c, first, box = _preview_thread(settings, ledger)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "yes")]
    e.tick()
    tid = f"{c['id']}__{_domain(first)}"
    at = ledger.thread(tid)["preview"]["requested_at"]
    box["msgs"] = [reply(first["To"], first["Message-ID"], "yes please")]
    e.tick()
    assert ledger.thread(tid)["preview"]["requested_at"] == at
    assert sum(1 for ev in ledger.events() if ev["kind"] == "preview_yes") == 1


def test_question_that_is_not_a_yes_gets_a_normal_reply(settings, ledger):
    llm = FakeLLM(classify={"class": "question", "confidence": 0.9, "referral_email": "", "summary": "s"})
    e, smtp, c, first, box = _preview_thread(settings, ledger, llm=llm)
    n0 = len(smtp.sent)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "Is this a real website or just a mockup?")]
    out = e.tick()
    assert out["replies"]["question"] == 1 and out["replies"]["preview_yes"] == 0
    assert len(smtp.sent) == n0 + 1
    assert not ledger.thread(f"{c['id']}__{_domain(first)}").get("preview")


def test_optout_wins_over_a_yes(settings, ledger):
    llm = FakeLLM(classify={"class": "preview_yes", "confidence": 0.99, "referral_email": "", "summary": "s"})
    e, smtp, c, first, box = _preview_thread(settings, ledger, llm=llm)
    n0 = len(smtp.sent)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "yes but take me off your list")]
    out = e.tick()
    assert out["replies"]["optout"] == 1 and out["replies"]["preview_yes"] == 0
    assert ledger.is_suppressed(email=first["To"])
    assert not ledger.thread(f"{c['id']}__{_domain(first)}").get("preview")
    assert len(smtp.sent) == n0


def test_without_a_preview_offer_a_yes_is_plain_interest(settings, ledger):
    llm = FakeLLM(classify={"class": "preview_yes", "confidence": 0.95, "referral_email": "", "summary": "s"})
    e, smtp, c, first, box = _preview_thread(settings, ledger, llm=llm, enabled=False)
    box["msgs"] = [reply(first["To"], first["Message-ID"], "yes")]
    out = e.tick()
    assert out["replies"]["interested"] == 1 and out["replies"]["preview_yes"] == 0
    assert not ledger.thread(f"{c['id']}__{_domain(first)}").get("preview")
