"""Task 7 end to end: a roofer campaign through prospect -> Email 1 -> replies -> preview API -> Email 2.

The preview API is a real HTTP stub (the shapes of pw-site-factory's previews.mjs / service.mjs), called
through the real PreviewClient over the wire. Mail goes to FakeSMTP. Three prospects:
  A replies "Sure, show me"            -> exactly one POST (no review fields) -> Email 2 in the same thread
  B replies "yes but stop emailing me" -> opt-out: suppressed, no POST, no Email 2
  C is tagged presence:customer        -> nothing at all after the tag, from any campaign
"""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FakeLLM
from leadgen.previews import PreviewClient
from test_engine import always_in_window, make_engine, reply, seed_active  # noqa: F401

KEY = "stub-preview-key"
REVIEWISH = ("rating", "review", "stars", "testimonial")


class Stub(BaseHTTPRequestHandler):
    posts: list = []
    records: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _keyed(self):
        if self.headers.get("X-Preview-Key") != KEY:
            self._send(401, {"error": "X-Preview-Key missing or wrong"})
            return False
        return True

    def do_POST(self):
        if not self._keyed():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.posts.append(body)
        if any(w in json.dumps(list(_keys(body))).lower() for w in REVIEWISH):
            return self._send(400, {"error": "previews never show ratings or reviews"})
        pid = "pv_" + hashlib.sha256(body["lead_id"].encode()).hexdigest()[:20]
        base = f"http://{self.headers['Host']}"
        Stub.records.setdefault(pid, {"preview_id": pid, "status": "ready", "expires_at": "2026-11-22T00:00:00Z",
                                      "preview_url": f"{base}/p/{pid}/", "screenshot_url": f"{base}/previews/{pid}/screenshot.png",
                                      "checkout_url": f"https://buy.example-ds.com/x?client_reference_id={pid}", "error": None})
        self._send(202, {"preview_id": pid, "status": "building", "industry_fallback": False})

    def do_GET(self):
        if not self._keyed():
            return
        pid = self.path.rstrip("/").split("/")[-1]
        rec = Stub.records.get(pid)
        self._send(200, rec) if rec else self._send(404, {"error": "no such preview"})


def _keys(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield k
            yield from _keys(x)
    elif isinstance(v, list):
        for x in v:
            yield from _keys(x)


def _preview_writer(model, system, user):
    """A writer that follows the preview rules: no link, the exact ask on step 1, the preview on later steps."""
    import re
    from leadgen.writer import PREVIEW_ASK
    name = re.search(r"Business: (.+?) \(", user).group(1)
    keys = re.findall(r"^- (fact:[a-z_]+):", user, re.M)
    if "Earlier email in this thread" in user:
        body = f"Hi {name},\n\nJust a short note: the preview of a new site for you is still here whenever you want a look.\n\nMarcus"
    else:
        body = (f"Hi {name},\n\nI was looking at local businesses in your area and spent a few minutes on your website. "
                "The pages did not resize on my phone, so they showed as a shrunken desktop page and were hard "
                f"to read.\n\nSo I put together a preview of a new site for you.\n\n{PREVIEW_ASK}\n\nMarcus")
    return {"subject": f"A new website for {name[:30]}", "body": body, "facts_used": [], "evidence_used": keys[:1]}


@pytest.fixture
def stub():
    Stub.posts, Stub.records = [], {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_roofer_campaign_end_to_end(settings, ledger, stub):
    settings["previews"]["email2_live"] = True          # stub mail only; the real gate stays off in settings.yaml
    tags = {}
    llm = FakeLLM(writer=_preview_writer)
    box = {"msgs": []}
    e, smtp = make_engine(settings, ledger, llm=llm, imap=lambda mb: box["msgs"],
                          ghl_tags=lambda email: tags.get(email, []))
    e._preview_client = PreviewClient(stub, KEY)
    c = seed_active(e, ledger, daily_cap=6)
    e._first_touch_target = lambda c: 3
    price = "The website is $600 to build, then $97 a month."
    ledger.save_campaign(c["id"], {"offer": {**c["offer"], "facts": c["offer"]["facts"] + [price]},
                                   "preview": {"enabled": True, "industry": "roofing", "price_fact": price},
                                   "icp": {**c["icp"], "site_filter": "weak"}})
    e.prospect()
    out = e.tick()
    assert out["send"]["sent"] == 3
    first = {m["To"]: m for m, _ in smtp.sent}
    a, b, cc = sorted(first)
    for addr in (a, b, cc):                              # weak-site reasons came through to each lead
        lead = ledger.lead(c["id"], addr.split("@")[1])
        assert lead["site_quality"]["weak"] and lead["maps"].get("rating")   # rating stored, never sent

    tags[cc] = ["presence:customer"]
    box["msgs"] = [reply(a, first[a]["Message-ID"], "Sure, show me"),
                   reply(b, first[b]["Message-ID"], "yes but stop emailing me")]
    n_before = len(smtp.sent)
    llm_before = len(llm.calls)
    out = e.tick()

    # A: one POST over HTTP, no review fields, then Email 2 in the same thread.
    assert len(Stub.posts) == 1 and Stub.posts[0]["lead_id"] == f"{c['id']}__{a.split('@')[1]}"
    assert not [k for k in _keys(Stub.posts[0]) if any(w in k.lower() for w in REVIEWISH)]
    assert out["replies"]["preview_yes"] == 1 and out["previews"]["email2_sent"] == 1
    new = smtp.sent[n_before:]
    assert [m["To"] for m, _ in new] == [a]
    e2, env = new[0]
    assert e2["Subject"] == "Re: " + first[a]["Subject"]
    assert e2["In-Reply-To"] == box["msgs"][0]["Message-ID"] and first[a]["Message-ID"] in e2["References"]
    htm = e2.get_body(("html",)).get_content()
    assert "screenshot.png" in htm and "See your preview" in htm and "Get started now" in htm and price in htm
    assert settings["replies"]["bcc"] in env
    assert len(llm.calls) == llm_before                  # nothing model-written on this path

    # B: opt-out wins over the yes.
    assert out["replies"]["optout"] == 1 and ledger.is_suppressed(email=b)
    assert not ledger.s.get("preview_queue", f"{c['id']}__{b.split('@')[1]}")

    # C: tagged; its follow-up comes due and nothing goes, in this or any campaign.
    lead_c = ledger.lead(c["id"], cc.split("@")[1])
    import leadgen.sender as sender_mod
    ledger.save_lead(c["id"], lead_c["domain"], {"seq": {**lead_c["seq"], "next_due": sender_mod.today_et().isoformat()}})
    e.prospect()
    box["msgs"] = []
    n_before = len(smtp.sent)
    assert "1" in ledger.lead(c["id"], lead_c["domain"])["seq"]["drafts"]   # C's follow-up is written and due
    e.tick()
    e.tick()
    assert [m["To"] for m, _ in smtp.sent[n_before:]] == []
    assert ledger.is_suppressed(email=cc)
    assert len(Stub.posts) == 1                          # still exactly one POST overall
