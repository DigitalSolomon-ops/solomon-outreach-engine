import copy
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadgen.capabilities import load_registry  # noqa: E402
from leadgen.config import load_settings  # noqa: E402
from leadgen.providers import Candidate, Page, QuotaExhausted  # noqa: E402
from leadgen.store import Ledger, LocalStore  # noqa: E402

MODELS = {"haiku": {"id": "claude-haiku-5-5", "input_per_mtok": 0.10, "output_per_mtok": 0.50},
          "sonnet": {"id": "claude-sonnet-5-5", "input_per_mtok": 2.0, "output_per_mtok": 10.0}}

HOME_HTML = """<html><head><script src="https://example-cdn.com/x.js"></script></head><body>
<h1>Acme Heating & Air</h1><p>Serving Gastonia 24/7. Call us.</p>
<a href="/contact-us">Contact</a><a href="mailto:info@acmehvac.com">Email</a>
<img src="logo@2x.png"> <p>sentry: 1234@sentry.io</p></body></html>"""
CONTACT_HTML = """<html><body><form action="/send"></form>
<p>Owner: bob.owner@acmehvac.com</p></body></html>"""

MAILBOX = {"email": "marcus@send.example-ds.com", "from_name": "Marcus Barber", "smtp_host": "smtp.x",
           "smtp_port": 587, "imap_host": "imap.x", "user": "u", "password": "p"}


class FakeSecrets:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, name):
        v = self.values.get(name)
        return json.dumps(v) if isinstance(v, dict) else v

    def get_json(self, name):
        v = self.values.get(name)
        return v if isinstance(v, dict) or v is None else json.loads(v)

    def list_names(self, known=None):
        return list(self.values)



class FakeMaps:
    kind = "maps"
    calls = 0

    def __init__(self, key=None, **_):
        pass

    def search(self, query, geo, limit):
        FakeMaps.calls += 1
        slug = query.split()[0].lower() + geo.split(",")[0].lower().replace(" ", "")
        return [Candidate(name=f"Acme {query} | {geo}", website=f"https://www.{slug}.com/",
                          phone="704-555-0100", source="fake", rating=4.6, review_count=31),
                Candidate(name="Yelp listing", website="https://www.yelp.com/biz/x", source="fake")], 1


class OutOfCredits:
    kind = "maps"

    def __init__(self, key=None, **_):
        pass

    def search(self, *a):
        raise QuotaExhausted("402 out of credits")


class FakeScrape:
    def __init__(self, **_):
        pass

    def fetch(self, url):
        domain = url.split("//")[1].split("/")[0].removeprefix("www.")
        if "contact" in url:
            return Page(url=url, html=CONTACT_HTML.replace("acmehvac.com", domain)), 1
        return Page(url=url, html=HOME_HTML.replace("acmehvac.com", domain),
                    links=["/contact-us", "https://facebook.com/acme"]), 1


class FakeVerify:
    def __init__(self, **_):
        pass

    def verify(self, email):
        return "ok", 1


class FakeSMTP:
    """Records every message and envelope; one instance shared across connections."""

    def __init__(self):
        self.sent = []          # (msg, to_addrs)
        self.fail_next = None

    def __call__(self, creds):
        outer = self

        class Conn:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def send(self_inner, msg, to_addrs=None):
                if outer.fail_next:
                    exc, outer.fail_next = outer.fail_next, None
                    raise exc
                outer.sent.append((msg, to_addrs))
                return {}
        return Conn()


class FakeLLM:
    """Routes by system prompt. Each role can be overridden with a callable or a dict."""

    def __init__(self, **overrides):
        self.calls = []
        self.o = overrides

    def __call__(self, model, system, user, max_tokens):
        role = self.role(system)
        self.calls.append((role, model))
        fn = self.o.get(role)
        if callable(fn):
            out = fn(model, system, user)
        elif fn is not None:
            out = fn
        else:
            out = self.default(role, model, system, user)
        return (out if isinstance(out, str) else json.dumps(out)), 1000, 200

    @staticmethod
    def role(system):
        for key, role in (("strict reviewer of one cold email", "critic"), ("You review one AI-written reply", "reply_critic"),
                          ("Classify a reply", "classify"), ("answers replies to", "reply"),
                          ("research brief", "brief"), ("FIRST NAME only", "owner"),
                          ("cold email strategist", "plan"), ("Suggest a short, honest reply", "suggest"),
                          ("RECURRING free tier", "scout"), ("write one short, plain, first-person cold email", "writer")):
            if key in system:
                return role
        return "unknown"

    def default(self, role, model, system, user):
        if role == "critic" or role == "reply_critic":
            return {"pass": True, "issues": []}
        if role == "brief":
            keys = re.findall(r"^- (fact:[a-z_]+):", user, re.M)
            k = keys[0] if keys else "fact:no_chat"
            return {"pillar": "Conversation", "pillar_why": "x", "strongest_observation": {"text": "t", "evidence": k},
                    "opening_angle": "open", "claims": [{"text": "t", "evidence": k}], "do_not_say": []}
        if role == "owner":
            return {"first_name": "", "role": "", "source_sentence": ""}
        if role == "classify":
            return {"class": "interested", "confidence": 0.95, "referral_email": "", "summary": "s"}
        if role == "reply":
            return {"body": "Thanks for getting back to me. Here is where you can pick a time: {{campaign_link}}\n\nMarcus",
                    "answerable": True}
        if role == "suggest":
            return {"body": "Suggested."}
        if role == "writer":
            name = re.search(r"Business: (.+?) \(", user).group(1)
            keys = re.findall(r"^- (fact:[a-z_]+):", user, re.M)
            subj = re.search(r'exactly "([^"]+)"', system)
            body = (f"Hi {name},\n\nI was looking at local businesses around Gastonia and spent a minute on your website. "
                    "When I looked I did not see a way to chat or text with you from the page. The audit is free and "
                    "takes about five minutes, then names which area to fix first. If it helps, it is here: "
                    "{{campaign_link}}\n\nMarcus")
            return {"subject": subj.group(1) if subj else f"A quick look at {name[:30]}'s website", "body": body,
                    "facts_used": ["offer_1"], "evidence_used": keys[:1]}
        return {}


@pytest.fixture
def settings():
    s = copy.deepcopy(load_settings())
    s["unsubscribe"]["base_url"] = "https://unsub.example-ds.com"
    return s


@pytest.fixture
def registry():
    return load_registry()


@pytest.fixture
def ledger(tmp_path):
    return Ledger(LocalStore(tmp_path / "state.json"))
