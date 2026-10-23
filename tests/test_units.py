from datetime import datetime, timedelta, timezone
from email import message_from_string

import pytest

from conftest import MODELS, FakeLLM, FakeMaps, FakeSecrets, OutOfCredits
from leadgen import campaigns as camp
from leadgen.allocate import allocate_queries, thompson_shares
from leadgen.capabilities import RegistryError, inventory, rotation_entries, validate_registry
from leadgen.compliance import ComplianceError, sign_token, verify_token
from leadgen.extract import choose_email, find_emails, read_site
from leadgen.llm import LLM, Budget, BudgetStop
from leadgen.mailboxes import domain_health, spamhaus_dbl, warmup_cap
from leadgen.prospect import clean_name
from leadgen.providers import BillingSignal, Page, ProviderError
from leadgen.replies import rule_class
from leadgen.rotation import CallFailed, NoProviderAvailable, Rotation
from leadgen.sender import build_outbound, due_date
from leadgen.store import period_key, utcnow
from leadgen.strategist import validate_model_plan
from leadgen.writer import LINK_TOKEN, Writer, escalation_reason, template_first_touch, validate

MB = {"id": "1", "email": "marcus@send.example-ds.com",
      "creds": {"email": "marcus@send.example-ds.com", "from_name": "Marcus Barber"}}


# ---------------------------------------------------------------- rotation
def test_rotation_uses_first_until_cap_then_next(ledger):
    entries = [{"name": "a", "kind": "maps", "secret": "ka", "cap": 2, "period": "month"},
               {"name": "b", "kind": "maps", "secret": "kb", "cap": 5, "period": "month"}]
    rot = Rotation("discovery", entries, {"a": FakeMaps, "b": FakeMaps}, FakeSecrets({"ka": "1", "kb": "2"}), ledger)
    used = [rot.call("search", "HVAC", "Gastonia, NC", 5)[1] for _ in range(4)]
    assert used == ["a", "a", "b", "b"]
    assert "free cap reached" in rot.skipped["a"]


def test_rotation_quota_error_marks_exhausted_and_moves_on(ledger):
    entries = [{"name": "dry", "kind": "maps", "secret": "k", "cap": 100},
               {"name": "b", "kind": "maps", "secret": "k", "cap": 100}]
    seen = []
    rot = Rotation("discovery", entries, {"dry": OutOfCredits, "b": FakeMaps}, FakeSecrets({"k": "x"}), ledger,
                   on_exhausted=lambda n, r, b: seen.append((n, b)))
    assert rot.call("search", "q", "g", 3)[1] == "b"
    assert ledger.usage("dry", "month")["exhausted"] is True and seen == [("dry", False)]
    rot2 = Rotation("discovery", entries, {"dry": OutOfCredits, "b": FakeMaps}, FakeSecrets({"k": "x"}), ledger)
    rot2.call("search", "q", "g", 3)
    assert rot2.skipped["dry"] == "provider reported quota exhausted"


def test_rotation_missing_secret_skipped_and_none_left(ledger):
    rot = Rotation("discovery", [{"name": "a", "kind": "maps", "secret": "missing", "cap": 5}], {"a": FakeMaps},
                   FakeSecrets(), ledger)
    with pytest.raises(NoProviderAvailable):
        rot.call("search", "q", "g", 1)


def test_rotation_per_input_failure_is_callfailed(ledger):
    class Broken:
        def __init__(self, **_): pass
        def fetch(self, url): raise ProviderError("site down")
    rot = Rotation("scrape", [{"name": "direct"}], {"direct": Broken}, FakeSecrets(), ledger)
    with pytest.raises(CallFailed):
        rot.call("fetch", "https://x.com")


def test_rotation_kind_preference(ledger):
    class Web(FakeMaps):
        kind = "web"
    entries = [{"name": "w", "kind": "web", "secret": "k", "cap": 9}, {"name": "m", "kind": "maps", "secret": "k", "cap": 9}]
    rot = Rotation("discovery", entries, {"w": Web, "m": FakeMaps}, FakeSecrets({"k": "x"}), ledger)
    assert rot.call("search", "q", "g", 1, kinds=["maps", "web"])[1] == "m"


def test_card_on_file_billing_signal_disables_for_period_and_notifies(ledger):
    class Bills:
        kind = "maps"
        def __init__(self, **_): pass
        def search(self, *a): raise BillingSignal("402 payment required")
    seen = []
    entries = [{"name": "here_discover", "kind": "maps", "secret": "k", "cap": 4500, "card_on_file": True}]
    rot = Rotation("discovery", entries, {"here_discover": Bills}, FakeSecrets({"k": "x"}), ledger,
                   on_exhausted=lambda n, r, b: seen.append(b))
    with pytest.raises(NoProviderAvailable):
        rot.call("search", "q", "g", 1)
    assert seen == [True] and ledger.usage("here_discover", "month")["billing_signal"] is True


def test_here_stops_at_4500_of_5000_trusting_the_higher_count(ledger, registry):
    """card_safety_ratio 0.9 x 5,000 free = 4,500; their usage API count wins when higher."""
    reported = {"n": 0}

    class Here:
        kind = "maps"
        def __init__(self, **_): pass
        def usage(self): return reported["n"]
        def search(self, q, g, n): return [], 1
    inv = inventory(registry, {"global-here"})
    entries = rotation_entries(registry, inv, "discovery", {"here_discover": Here})
    assert entries[0]["cap"] == 4500
    ledger.add_usage("here_discover", "month", 4499)
    rot = Rotation("discovery", entries, {"here_discover": Here}, FakeSecrets({"global-here": "k"}), ledger)
    rot.call("search", "q", "g", 1)                      # unit 4,500
    with pytest.raises(NoProviderAvailable):
        rot.call("search", "q", "g", 1)
    assert "4500/4500" in rot.skipped["here_discover"]
    # Their own count is higher than ours: trust theirs.
    ledger2 = type(ledger)(type(ledger.s)(ledger.s.path.with_name("b.json")))
    reported["n"] = 4600
    rot2 = Rotation("discovery", entries, {"here_discover": Here}, FakeSecrets({"global-here": "k"}), ledger2)
    with pytest.raises(NoProviderAvailable):
        rot2.call("search", "q", "g", 1)


def test_once_credits_only_after_recurring(registry):
    names = {"global-millionverifier", "global-mailboxvalidator", "global-zerobounce"}
    adapters = {"millionverifier": object, "mailboxvalidator": object, "zerobounce": object}
    order = [e["name"] for e in rotation_entries(registry, inventory(registry, names), "verify", adapters)]
    assert order[-1] == "millionverifier" and order[:2] == ["mailboxvalidator", "zerobounce"]
    later = datetime(2027, 3, 1, tzinfo=timezone.utc)
    assert period_key("once") == period_key("once", later) == "lifetime"


# ---------------------------------------------------------------- capabilities
def test_registry_schema_rejects_bad_period(registry):
    import copy
    bad = copy.deepcopy(registry)
    bad["providers"][0]["free"]["period"] = "fortnight"
    with pytest.raises(RegistryError, match="providers/0/free/period"):
        validate_registry(bad)
    dup = copy.deepcopy(registry)
    dup["providers"].append(dup["providers"][0])
    with pytest.raises(RegistryError, match="duplicate"):
        validate_registry(dup)


def test_inventory_connected_vs_recommended(registry):
    present = {"GOOGLE_PLACES_API_KEY", "global-hunter", "US-Census-Bureau", "email-verifier-api-key",
               "global-leadgen-mailbox-1", "Robinhood_api"}
    inv = inventory(registry, present)
    conn = set(inv["connected"])
    assert {"google_places", "hunter_domain", "hunter_verify", "census_cbp", "jina_reader", "mx_check"} <= conn
    assert "zerobounce" not in conn and "verifalia" not in conn            # skip is never listed
    rec = {r["name"]: r for r in inv["recommended"]}
    assert rec["zerobounce"]["vault_slot"] == "global/zerobounce"
    assert "verifalia" not in rec and "google_places" not in rec
    assert inv["unassigned_secret_names"] == [] and "anymailfinder" in inv["connected"]
    assert "Robinhood_api" not in inv["matched_secret_names"]               # names only, and only ours
    assert inv["mailbox_secrets"] == ["global-leadgen-mailbox-1"]


# ---------------------------------------------------------------- extract
def test_find_and_choose_email_filters_junk():
    page = Page(url="https://acme.com/", html='<a href="mailto:info@acme.com">x</a> logo@2x.png '
                'noreply@acme.com bob@gmail.com 99@sentry.io user@example.com')
    emails = find_emails(page)
    assert "noreply@acme.com" not in emails and "99@sentry.io" not in emails
    assert choose_email(emails, "acme.com") == "info@acme.com"


def test_harvest_notice_excludes():
    sr = read_site([Page(url="https://a.com/", html="<p>Email addresses on this site may not be "
                         "used for commercial email.</p> info@a.com")])
    assert sr.harvest_notice


def test_stripped_html_makes_no_absence_claims():
    sr = read_site([Page(url="https://a.com/", html="<div>We are open 24/7</div>")])
    assert [f["id"] for f in sr.facts] == ["after_hours"]


@pytest.mark.parametrize("title,domain,expect", [
    ("HVAC Company in Gastonia, NC | Pine Ridge Company", "pineridgecompany.com", "Pine Ridge Company"),
    ("Gastonia HVAC Company | Bright Air Heating & Cooling", "brightairhvac.com", "Bright Air Heating & Cooling"),
    ("Falcon HVAC", "callfalconhvac.com", "Falcon HVAC"),
])
def test_clean_name_from_real_titles(title, domain, expect):
    assert clean_name(title, domain) == expect


# ---------------------------------------------------------------- writer: validator v2, critic, tiers
@pytest.fixture
def campaign(settings):
    return camp.seed_from_icp(settings)


@pytest.fixture
def lead():
    return {"name": "Acme HVAC", "segment": "HVAC", "geo": "Gastonia, NC", "domain": "acme.com",
            "facts": [{"id": "no_chat", "pillar": "Conversation", "text": "I did not see a way to chat."}]}


EV = {"fact:no_chat": "When I looked at the website I did not see a way to chat."}
GOOD = ("Hi Acme HVAC, I was looking at HVAC companies around Gastonia and spent a minute on your "
        "website. When I looked I did not see a way to chat or text with you from the page. "
        "The free AI Readiness Audit takes about five minutes and points to which of four areas to fix first. "
        f"If it helps, it is here: {LINK_TOKEN} Marcus")


def _v(d, campaign, lead, step=0, ev=EV, first=None):
    return validate(d, campaign, lead, ev, step, max_words=130, min_words=40, first_subject=first)


def test_validator_v2_accepts_clean_draft(campaign, lead):
    d = {"subject": "A quick look at Acme HVAC's website", "body": GOOD, "evidence_used": ["fact:no_chat"],
         "facts_used": ["offer_1"]}
    assert _v(d, campaign, lead) == []


@pytest.mark.parametrize("mutation,expect", [
    (lambda b: b + " See https://evil.com", "extra link"),
    (lambda b: b + " Visit acme-tools.com", "extra link"),
    (lambda b: b.replace("five minutes", "7 minutes"), "numbers not in facts"),
    (lambda b: b + " We guarantee results.", "banned phrase"),
    (lambda b: b.replace(LINK_TOKEN, ""), "exactly once"),
    (lambda b: b + " Only $99.", "price talk"),
    (lambda b: b.replace("Hi Acme HVAC", "Hi Bob"), "greeting names someone"),
    (lambda b: b + " Your 4.8 star reviews are great.", "reviews or ratings"),
    (lambda b: b.replace("website.", "website — nice."), "dashes"),
])
def test_validator_v2_rejects(campaign, lead, mutation, expect):
    d = {"subject": "Quick look", "body": mutation(GOOD), "evidence_used": ["fact:no_chat"]}
    assert any(expect in p for p in _v(d, campaign, lead))


def test_validator_v2_followup_keeps_subject_and_rejects_fake_re(campaign, lead):
    d = {"subject": "Re: Quick look", "body": "Hi Acme HVAC, one more short note on the audit. "
         f"It is free and takes about five minutes: {LINK_TOKEN} Marcus", "evidence_used": []}
    probs = _v(d, campaign, lead, step=1, first="Quick look")
    assert any("keep the first email's subject" in p for p in probs)
    assert any("reply/forward" in p for p in probs)


def test_validator_v2_owner_name_must_be_cited(campaign, lead):
    lead = {**lead, "owner": {"first_name": "Dana", "source_sentence": "Owner Dana Lee founded Acme."}}
    ev = {**EV, "owner:first_name": "Dana (owner): Owner Dana Lee founded Acme."}
    d = {"subject": "Quick look", "body": GOOD.replace("Hi Acme HVAC", "Hi Dana"), "evidence_used": ["fact:no_chat"]}
    assert any("owner:first_name" in p for p in _v(d, campaign, lead, ev=ev))
    d["evidence_used"].append("owner:first_name")
    assert _v(d, campaign, lead, ev=ev) == []


def _writer(settings, ledger, fake):
    from leadgen.llm import LLM, Budget
    return Writer(LLM(None, MODELS, ledger, Budget(settings, ledger), client=fake), settings, ledger)


def test_critic_failure_goes_to_sonnet_then_template_for_first_touch(settings, ledger, campaign, lead):
    fake = FakeLLM(critic={"pass": False, "issues": ["pushy"]})
    out = _writer(settings, ledger, fake).write(campaign, lead, EV, {"pillar": "Conversation"}, step=0)
    writers = [m for r, m in fake.calls if r == "writer"]
    assert writers == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert out["source"] == "template"
    assert _v(out, campaign, lead) == [] or all("length" in p for p in _v(out, campaign, lead))


def test_critic_failure_then_sonnet_passes(settings, ledger, campaign, lead):
    verdicts = iter([{"pass": False, "issues": ["flattering"]}, {"pass": True, "issues": []}])
    fake = FakeLLM(critic=lambda m, s, u: next(verdicts))
    out = _writer(settings, ledger, fake).write(campaign, lead, EV, {}, step=0)
    assert out["source"] == "sonnet-rewrite" and out["tier"] == "sonnet"


def test_critic_failure_skips_followup(settings, ledger, campaign, lead):
    from leadgen.writer import SkipEmail
    fake = FakeLLM(critic={"pass": False, "issues": ["no"]})
    thread = [{"step": 0, "subject": "A quick look", "body": "first"}]
    with pytest.raises(SkipEmail):
        _writer(settings, ledger, fake).write(campaign, lead, EV, {}, step=1, thread=thread)


def test_template_passes_its_own_rules(campaign, lead):
    t = template_first_touch(campaign, lead, EV)
    assert [p for p in _v(t, campaign, lead) if "length" not in p] == []


def test_auto_escalation_triggers(settings, ledger, campaign):
    assert escalation_reason(settings, ledger, campaign, 149, 0) is None
    assert "positive-reply rate" in escalation_reason(settings, ledger, campaign, 150, 1)
    assert escalation_reason(settings, ledger, campaign, 150, 2) is None          # 1.33% >= 1.0%
    ledger.set_state(f"writer_stats_{campaign['id']}", critic_window=[True] * 34 + [False] * 16)
    assert "failed the critic" in escalation_reason(settings, ledger, campaign, 10, 0)
    ledger.set_state(f"writer_stats_{campaign['id']}", critic_window=[True] * 35 + [False] * 15)
    assert escalation_reason(settings, ledger, campaign, 10, 0) is None           # exactly 30% is not "more than"
    sonnet = {**campaign, "settings": {**campaign["settings"], "writer_tier": "sonnet"}}
    assert escalation_reason(settings, ledger, sonnet, 500, 0) is None            # never switches down


# ---------------------------------------------------------------- strategist plan schema
def _plan(**over):
    p = {"icp_cells": [{"segment": "HVAC", "geo": "Gastonia, NC", "query": "HVAC company", "naics": "238220"}],
         "offer": {"name": "Audit", "facts": ["It is free."], "faq": [{"q": "Cost?", "a": "It is free."}]},
         "source_plan": {"discover_maps": ["google_places", "serpapi_maps"], "verify_email": ["zerobounce"]},
         "sequence": [{"step": 0, "day": 0, "angle": "a"}, {"step": 1, "day": 3, "angle": "b"}],
         "risks": [], "recommended_connections": [{"name": "zerobounce", "why": "verify"}]}
    p.update(over)
    return p


def test_plan_schema_accepts_valid_and_rejects_unknown_provider_and_field(registry):
    assert validate_model_plan(_plan(), registry) == []
    bad = _plan(source_plan={"discover_maps": ["acme_magic_leads"]})
    assert any("unknown provider 'acme_magic_leads'" in p for p in validate_model_plan(bad, registry))
    assert any("skip" in p for p in validate_model_plan(_plan(source_plan={"verify_email": ["verifalia"]}), registry))
    assert any("schema" in p for p in validate_model_plan(_plan(estimated_leads=5000), registry))
    assert any("unknown provider" in p for p in validate_model_plan(
        _plan(recommended_connections=[{"name": "nope", "why": "x"}]), registry))


# ---------------------------------------------------------------- budget
def test_budget_80_100_120(settings, ledger):
    b = Budget(settings, ledger)
    llm = LLM(None, MODELS, ledger, b, client=lambda *a: ("{}", 0, 0))
    ledger.record_cost({"purpose": "first_touch", "usd": 23.9})
    b.check_notices()
    assert not any(e["kind"] == "budget" for e in ledger.events())
    ledger.record_cost({"purpose": "first_touch", "usd": 0.2})        # 24.1 = 80.3%
    b.check_notices()
    assert [e["what"][:40] for e in ledger.events() if e["kind"] == "budget"] == ["AI spend reached 80% of the $30 monthly "]
    with pytest.raises(BudgetStop, match="held for replies"):        # non-reply over 80% of cap (reserve)
        llm.call("haiku", "s", "u", purpose="first_touch")
    llm.call("haiku", "s", "u", purpose="reply_write")                 # replies still run
    ledger.record_cost({"purpose": "reply_write", "usd": 6.0})        # 30.1 total = 100%
    b.check_notices()
    assert sum(1 for e in ledger.events() if e["kind"] == "budget") == 2
    with pytest.raises(BudgetStop, match="monthly cap"):
        llm.call("haiku", "s", "u", purpose="follow_up")
    llm.call("haiku", "s", "u", purpose="reply_classify")              # up to 120%
    ledger.record_cost({"purpose": "reply_write", "usd": 6.0})        # 36.1 = 120.3%
    with pytest.raises(BudgetStop, match="120%"):
        llm.call("haiku", "s", "u", purpose="reply_write")


def test_llm_records_cost_from_tokens(settings, ledger):
    llm = LLM(None, MODELS, ledger, Budget(settings, ledger), client=lambda *a: ("hi", 1_000_000, 100_000))
    llm.call("sonnet", "s", "u", purpose="plan", campaign_id="c1")
    [row] = ledger.costs_since(utcnow() - timedelta(minutes=1))
    assert row["usd"] == pytest.approx(2.0 + 1.0) and row["input_tokens"] == 1_000_000 and row["campaign_id"] == "c1"


# ---------------------------------------------------------------- Thompson
def test_thompson_floor_until_100_sends():
    import random
    stats = {"hot": {"sends": 300, "positive": 30}, "cold": {"sends": 40, "positive": 0},
             "new": {"sends": 0, "positive": 0}, "dead": {"sends": 300, "positive": 0}}
    shares = thompson_shares(stats, floor=0.05, floor_until=100, rng=random.Random(7))
    assert sum(shares.values()) == pytest.approx(1)
    assert shares["cold"] >= 0.05 - 1e-9 and shares["new"] >= 0.05 - 1e-9
    assert shares["dead"] < 0.05          # past 100 sends: no floor
    assert shares["hot"] > shares["cold"] and shares["hot"] > shares["dead"]
    q = allocate_queries(shares, 20)
    assert sum(q.values()) == 20 and q["cold"] >= 1 and q["new"] >= 1


# ---------------------------------------------------------------- deliverability
class FakeResolver:
    def __init__(self, txt=None, a=None):
        self.txt, self.a = txt or {}, a or {}

    def resolve(self, name, rtype, lifetime=10):
        import dns.resolver
        table = self.txt if rtype == "TXT" else self.a
        if name not in table:
            raise dns.resolver.NXDOMAIN()

        class R:
            def __init__(s, v): s.v = v; s.strings = [v.encode()]
            def to_text(s): return s.v
        return [R(v) for v in table[name]]


def test_spamhaus_public_resolver_code_is_unknown_not_listed():
    assert spamhaus_dbl("x.com", FakeResolver(a={"x.com.dbl.spamhaus.org": ["127.255.255.254"]})) == "unknown"
    assert spamhaus_dbl("x.com", FakeResolver(a={"x.com.dbl.spamhaus.org": ["127.0.1.2"]})) == "listed"
    assert spamhaus_dbl("x.com", FakeResolver()) == "clean"


def test_domain_health_needs_spf_dkim_dmarc():
    txt = {"send.com": ["v=spf1 include:_spf.google.com ~all"], "_dmarc.send.com": ["v=DMARC1; p=none"],
           "google._domainkey.send.com": ["v=DKIM1; k=rsa; p=MIIB"]}
    h = domain_health("send.com", resolver=FakeResolver(txt=txt, a={"send.com.dbl.spamhaus.org": ["127.255.255.252"]}))
    assert h["ok"] and h["dkim_selector"] == "google" and h["dbl"] == "unknown"
    del txt["_dmarc.send.com"]
    assert not domain_health("send.com", resolver=FakeResolver(txt=txt))["ok"]


def test_warmup_ramp():
    cfg = {"warmup_start": 5, "warmup_ramp_per_day": 2, "warmup_max": 30}
    now = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)
    assert warmup_cap(cfg, None, now) == 5
    assert warmup_cap(cfg, now - timedelta(days=3), now) == 11
    assert warmup_cap(cfg, now - timedelta(days=40), now) == 30


def test_sequence_due_dates_skip_weekends():
    from datetime import date
    fri = date(2026, 10, 23)
    assert due_date(fri, 0) == fri
    assert due_date(fri, 3).isoformat() == "2026-10-26"       # Monday
    assert due_date(fri, 7).isoformat() == "2026-10-30"


# ---------------------------------------------------------------- compliance
def test_token_roundtrip_and_tamper():
    tok = sign_token("Info@Acme.com", "s3cret")
    assert verify_token(tok, "s3cret") == "info@acme.com"
    assert verify_token(tok, "other") is None


def test_outbound_has_canspam_parts_and_threading(settings):
    msg = build_outbound(settings, MB, "info@acme.com", "Hello", f"Hi there {LINK_TOKEN} Marcus",
                         link="https://digitalsolomon.com/audit?utm_source=leadgen", unsub_url="https://u.example/u/t",
                         in_reply_to="<a@x>", references=["<a@x>"])
    text = msg.get_body(("plain",)).get_content()
    assert settings["send"]["postal_address"] in text and "advertisement" in text
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and msg["In-Reply-To"] == "<a@x>"
    assert "{{" not in text and "Bcc" not in msg


def test_outbound_blocks_missing_unsubscribe(settings):
    with pytest.raises(ComplianceError):
        build_outbound(settings, MB, "a@b.com", "Hi", f"x {LINK_TOKEN}", link="https://d.com", unsub_url="")


def test_rule_classes():
    m = lambda frm, subj, body, hdr="": message_from_string(f"From: {frm}\nSubject: {subj}\n{hdr}\n{body}")
    assert rule_class(m("Bob <bob@acme.com>", "Re: hi", "Please remove me"))[0] == "optout"
    assert rule_class(m("Bob <bob@acme.com>", "Re: hi", "Sounds good.\n\nOn Tue Marcus wrote:\n> unsubscribe"))[0] is None
    assert rule_class(m("Bob <bob@acme.com>", "Out of Office", "I am away", "Auto-Submitted: auto-replied\n"))[0] == "auto"
    kind, addr = rule_class(m("MAILER-DAEMON@x.com", "Undeliverable", "Final-Recipient: rfc822; gone@acme.com"))
    assert kind == "bounce" and addr == "gone@acme.com"


# ---------------------------------------------------------------- campaigns
def test_seed_campaign_is_draft_with_landing_and_cells(settings):
    c = camp.seed_from_icp(settings)
    assert c["status"] == "draft" and c["landing_url"] == "https://digitalsolomon.com/audit"
    assert c["booking_url"] == "https://digitalsolomon.com/contact"
    assert len(c["icp"]["cells"]) == 54 and c["offer"]["facts"][0] == "The AI Readiness Audit is free."
    link = camp.campaign_link(c, "acme.com", 2)
    assert "utm_campaign=ai-readiness-audit-gaston-charlotte" in link and "utm_content=acme.com" in link
    assert link.endswith("utm_term=step2")


def test_campaign_requires_landing_url(settings):
    c = camp.new_campaign(settings, name="X", description="d", landing_url="", offer={"name": "o"})
    with pytest.raises(camp.CampaignError, match="landing_url"):
        camp.validate(c)
    with pytest.raises(camp.CampaignError, match="can't be edited"):
        camp.apply_patch(camp.seed_from_icp(settings), {"status": "active"})


def test_network_timeout_falls_through_to_next_provider(ledger):
    import httpx

    class Slow:
        def __init__(self, **_): pass
        def fetch(self, url): raise httpx.ReadTimeout("slow")
    from conftest import FakeScrape
    rot = Rotation("scrape", [{"name": "slow"}, {"name": "ok"}], {"slow": Slow, "ok": FakeScrape}, FakeSecrets(), ledger)
    page, name = rot.call("fetch", "https://acme.com/")
    assert name == "ok" and "ReadTimeout" in rot.errors[0]
