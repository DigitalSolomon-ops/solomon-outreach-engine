"""Email 1 for preview campaigns: plain text, no links, ends with the exact preview question."""
import re

from conftest import FakeLLM
from leadgen.writer import LINK_TOKEN, PREVIEW_ASK, template_first_touch, validate
from test_engine import always_in_window, make_engine, seed_active  # noqa: F401

CAMPAIGN = {"id": "roof", "offer": {"name": "Website preview", "facts": ["I build a free preview of a new website for your business."],
                                    "sender_name": "Marcus Barber"},
            "preview": {"enabled": True, "industry": "roofing"}, "sequence": [{"step": 0, "day": 0, "angle": "a"}]}
LEAD = {"name": "Acme Roofing", "geo": "Gastonia, NC", "segment": "Roofers", "domain": "acmeroofing.com"}
EVIDENCE = {"fact:weak_not_mobile": "The website's pages are not set up to resize for phones, so on a phone they show as a shrunken desktop page."}
GOOD = ("Hi Acme Roofing,\n\nI was looking at roofers around Gastonia and spent a few minutes on your website. "
        "On my phone the pages did not resize, so they showed as a shrunken desktop page, which makes it hard "
        "to read about your work or find how to reach you.\n\nSo I put together a preview of what a new site "
        f"for Acme Roofing could look like.\n\n{PREVIEW_ASK}\n\nMarcus")


def _v(body, step=0, subject="A new website for Acme Roofing", first_subject=None):
    return validate({"subject": subject, "body": body, "evidence_used": list(EVIDENCE)}, CAMPAIGN, LEAD, EVIDENCE,
                    step, max_words=130, min_words=40, first_subject=first_subject)


def test_good_preview_email_passes():
    assert _v(GOOD) == []


def test_preview_email_rejects_any_link_and_needs_the_exact_ask():
    assert "preview emails carry no link" in _v(GOOD.replace(PREVIEW_ASK, f"{PREVIEW_ASK} {LINK_TOKEN}"))
    assert any("extra link" in p for p in _v(GOOD.replace("Marcus", "acmepreview.com\n\nMarcus")))
    assert any("must ask exactly" in p for p in _v(GOOD.replace(PREVIEW_ASK, "Want a look at what I made?")))


def test_preview_follow_up_must_be_about_the_preview():
    s = "A new website for Acme Roofing"
    assert _v("Hi Acme Roofing,\n\nJust floating this back up. Happy to send it over if you want a look.\n\nMarcus",
              step=1, subject=s, first_subject=s) == ["follow-up must be about the preview"]
    assert _v("Hi Acme Roofing,\n\nThe preview of your new site is still here if you want a look.\n\nMarcus",
              step=1, subject=s, first_subject=s) == []


def test_non_preview_campaigns_still_need_the_link():
    c = {**CAMPAIGN, "preview": {"enabled": False}}
    probs = validate({"subject": "A look at Acme", "body": GOOD, "evidence_used": list(EVIDENCE)}, c, LEAD, EVIDENCE, 0,
                     max_words=130, min_words=40)
    assert "campaign link token must appear exactly once" in probs


def test_preview_template_passes_its_own_validator():
    t = template_first_touch(CAMPAIGN, LEAD, EVIDENCE)
    assert t["body"].rstrip().endswith(f"{PREVIEW_ASK}\n\nMarcus") and LINK_TOKEN not in t["body"]
    assert validate(t, CAMPAIGN, LEAD, EVIDENCE, 0, max_words=130, min_words=40) == []


def _preview_campaign(e, ledger):
    c = seed_active(e, ledger, daily_cap=2)
    ledger.save_campaign(c["id"], {"preview": {"enabled": True, "industry": "roofing", "price_fact": c["offer"]["facts"][0]},
                                   "icp": {**c["icp"], "site_filter": "weak"}})
    return ledger.campaign(c["id"])


def test_email1_goes_out_plain_text_with_the_ask_and_no_link(settings, ledger):
    llm = FakeLLM()                     # its default draft carries a link token, so the template ships
    e, smtp = make_engine(settings, ledger, llm=llm)
    c = _preview_campaign(e, ledger)
    e.prospect()
    e.tick()
    assert smtp.sent
    for msg, _ in smtp.sent:
        assert not msg.is_multipart() and msg.get_content_type() == "text/plain"      # no HTML, no images
        text = msg.get_content()
        body, footer = text.split("\n--\n", 1)
        assert PREVIEW_ASK in body and not re.search(r"https?://|www\.", body)
        assert "utm_" not in text and "/u/" in footer and "100 Example St" in footer
        assert msg["List-Unsubscribe"]


def test_model_written_preview_email_is_accepted(settings, ledger):
    def writer(model, system, user):
        assert "No links, URLs, domain names or tokens" in system and PREVIEW_ASK in system
        name = re.search(r"Business: (.+?) \(", user).group(1)
        key = re.findall(r"^- (fact:[a-z_]+):", user, re.M)[0]
        body = (f"Hi {name},\n\nI was looking at roofers in your area and spent a few minutes on your website. "
                "The pages did not resize on my phone, so they showed as a shrunken desktop page, and it was "
                f"hard to find how to reach you.\n\nSo I put together a preview of a new site for you.\n\n{PREVIEW_ASK}\n\nMarcus")
        return {"subject": f"A new website for {name[:30]}", "body": body, "facts_used": [], "evidence_used": [key]}
    e, smtp = make_engine(settings, ledger, llm=FakeLLM(writer=writer))
    c = _preview_campaign(e, ledger)
    e.prospect()
    leads = ledger.leads(c["id"], "in_sequence")
    assert leads and all(l["seq"]["drafts"]["0"]["source"].startswith("model") for l in leads)


def _live_like_critic(model, system, user):
    """What the live critic did on 2026-10-24 (Roofers preflight 0/5): it failed every Email 1 for
    the fixed ask, unless told the operator approved that line."""
    if PREVIEW_ASK in user and "approved that line word for word" not in system:
        return {"pass": False, "issues": [f"The closing line '{PREVIEW_ASK}' implies a preview already exists."]}
    return {"pass": True, "issues": []}


def _good_writer(model, system, user):
    return {"subject": "A new website for Acme Roofing", "body": GOOD, "facts_used": [],
            "evidence_used": list(EVIDENCE)}


def test_critic_does_not_fail_the_approved_preview_ask(settings, ledger):
    e, _ = make_engine(settings, ledger, llm=FakeLLM(writer=_good_writer, critic=_live_like_critic))
    draft = e.writer.write(CAMPAIGN, LEAD, EVIDENCE, {}, step=0)
    assert draft["source"] == "model-haiku" and draft["attempts"] == []


def test_critic_note_is_only_for_preview_campaigns(settings, ledger):
    seen = []
    e, _ = make_engine(settings, ledger, llm=FakeLLM(critic=lambda m, s, u: seen.append(s) or {"pass": True, "issues": []}))
    draft = {"subject": "s", "body": "b"}
    e.writer.critic(CAMPAIGN, draft, EVIDENCE, {})
    e.writer.critic({**CAMPAIGN, "preview": {"enabled": False}}, draft, EVIDENCE, {})
    assert PREVIEW_ASK in seen[0] and PREVIEW_ASK not in seen[1]


def test_samples_carry_each_rejection_reason(settings, ledger):
    from leadgen.prospect import Prospector
    fail = lambda m, s, u: {"pass": False, "issues": ["says something odd"]}
    def writer(model, system, user):
        name = re.search(r"Business: (.+?) \(", user).group(1)
        key = re.findall(r"^- (fact:[a-z_]+):", user, re.M)[0]
        return {"subject": f"A new website for {name[:30]}", "body": GOOD.replace("Acme Roofing", name),
                "facts_used": [], "evidence_used": [key]}
    e, _ = make_engine(settings, ledger, llm=FakeLLM(writer=writer, critic=fail))
    c = _preview_campaign(e, ledger)
    out = Prospector(e).samples(c, n=1)
    assert out and not out[0]["passed"]
    assert out[0]["attempts"] == [["critic: says something odd"], ["critic: says something odd"]]
