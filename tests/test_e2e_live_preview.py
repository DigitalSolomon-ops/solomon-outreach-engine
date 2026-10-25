"""Task 7 against the LIVE preview API (pw-site-factory). Skipped unless LEADGEN_LIVE_PREVIEW=1.

Real PreviewClient + real key from Secret Manager; mail is FakeSMTP and state is a temp file, so nothing
is emailed and production Firestore is untouched. The lead is a fake roofer in a fictional city
(the funnel's own tests use Testville so real cities' hero images are not used up).
"""
import os

import pytest

from conftest import FakeLLM
from leadgen.config import load_settings
from leadgen.previews import PreviewClient
from test_engine import always_in_window, make_engine, reply, seed_active  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("LEADGEN_LIVE_PREVIEW") != "1", reason="live preview API test")


def _key():
    from google.cloud import secretmanager
    name = load_settings()["previews"]["key_secret"]
    c = secretmanager.SecretManagerServiceClient()
    return c.access_secret_version(name=f"projects/your-gcp-project/secrets/{name}/versions/latest").payload.data.decode().strip()


def test_live_yes_to_email2(settings, ledger):
    import time
    settings["previews"]["email2_live"] = True          # FakeSMTP only
    llm = FakeLLM()
    box = {"msgs": []}
    e, smtp = make_engine(settings, ledger, llm=llm, imap=lambda mb: box["msgs"])
    e._preview_client = PreviewClient(settings["previews"]["base_url"], _key(), timeout=60)
    c = seed_active(e, ledger, daily_cap=1)
    price = "The website is $600 to build, then $97 a month."
    ledger.save_campaign(c["id"], {"id": "leadgen-e2e-live", "offer": {**c["offer"], "facts": c["offer"]["facts"] + [price]},
                                   "preview": {"enabled": True, "industry": "roofing", "price_fact": price}})
    e.prospect()
    e.tick()
    first = smtp.sent[0][0]
    dom = first["To"].split("@")[1]
    ledger.save_lead(c["id"], dom, {"geo": "Testville, NC", "name": "Leadgen E2E Summit Roofing"})
    box["msgs"] = [reply(first["To"], first["Message-ID"], "Sure, show me")]
    e.tick()
    box["msgs"] = []
    t0 = time.time()
    while time.time() - t0 < 240:
        q = ledger.s.get("preview_queue", f"{c['id']}__{dom}")
        if q["status"] not in ("queued", "requested"):
            break
        time.sleep(10)
        e.tick()
    q = ledger.s.get("preview_queue", f"{c['id']}__{dom}")
    print("\nqueue item:", {k: q.get(k) for k in ("status", "preview_id", "preview_url", "screenshot_url", "checkout_url", "held_reason")})
    assert q["status"] == "sent", q
    e2 = [m for m, _ in smtp.sent if "Get started now" in m.get_body(("html",)).get_content()]
    assert len(e2) == 1 and e2[0]["In-Reply-To"]
    print("Email 2 subject:", e2[0]["Subject"])
