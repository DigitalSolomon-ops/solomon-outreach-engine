"""Create the new-business trigger campaign (Website Launch, strategy new_business) as a DRAFT.

Targets maps listings in one industry with NO website and at most --max-reviews reviews: the
businesses that most likely just opened. Offer: Website Launch with the free preview. Goes
through the engine API's own POST /campaigns and PATCH (same validation The Creator uses), in
process against the live store. Idempotent by name. Nothing launches; a draft never sends.

  python scripts/create_new_business_campaign.py --industry auto-detailing [--max-reviews 5] [--dry-run]

Requires the engine build with the strategy registry (deploy first, then run this).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadgen import industries as inds  # noqa: E402

PRICE = "The website is $600 to build, then $97 a month."
PREVIEW_FACT = "I build a free preview of a new website for your business, so you can see it before deciding anything."
LIVE_FACT = "Once you approve it, the site can be live in 3 days."
# The angles never claim the business is new: a few reviews is a signal, not a fact we can cite.
SEQUENCE = [
    {"step": 0, "day": 0, "angle": "First touch: you looked for them online and found the listing but no website; "
                                   "customers who search for them have nowhere to land. Then the preview question. "
                                   "Do not say or imply they just opened."},
    {"step": 1, "day": 3, "angle": "Short bump: the preview is ready to build whenever they want a look; ask again in your own words."},
    {"step": 2, "day": 7, "angle": "Why a site matters when people find a listing with no website: one plain, true reason "
                                   "tied to their trade. Offer the preview again."},
    {"step": 3, "day": 14, "angle": "Polite close: last note about the preview, easy to say no, no pressure."},
]


def plan(slug: str, max_reviews: int) -> dict:
    ind = inds.get(slug)
    cat = inds.catalog()
    label = ind["label"]
    return {
        "create": {
            "name": f"New Business · Website Launch · {label} · {cat['area_label']}",
            "description": f"New-business trigger: {label.lower()} around {cat['area_label']} with a Google listing, "
                           f"no website and {max_reviews} or fewer reviews. Email 1 offers a free preview; "
                           "a yes gets it built and sent as Email 2.",
            "landing_url": cat["website_launch"]["landing_url"].format(slug=slug),
            "booking_url": cat["website_launch"]["booking_url"].format(slug=slug),
            "offer_name": "Website Launch",
        },
        "patch": {
            "offer": {"name": "Website Launch",
                      "facts": [PREVIEW_FACT, ind["h1"].strip(), LIVE_FACT, PRICE],
                      "faq": [{"q": q["q"], "a": q["a"]} for q in ind.get("faq", [])][:4]},
            "preview": {"enabled": True, "industry": slug, "price_fact": PRICE},
            "icp": {"cells": inds.cells_for(ind), "strategy": "new_business",
                    "strategy_params": {"max_reviews": max_reviews}},
            "sequence": SEQUENCE,
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--industry", required=True)
    ap.add_argument("--max-reviews", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    it = plan(a.industry, a.max_reviews)
    if a.dry_run:
        print(json.dumps(it, indent=1))
        return 0
    from fastapi.testclient import TestClient

    from leadgen.api import create_app
    from leadgen.engine import make_engine
    engine = make_engine()
    client = TestClient(create_app(lambda: engine))
    name = it["create"]["name"]
    existing = {c["name"]: c for c in client.get("/campaigns").json()["campaigns"]}
    if name in existing:
        print(f"skip   exists as {existing[name]['id']} ({existing[name]['status']})")
        return 0
    r = client.post("/campaigns", json=it["create"])
    if r.status_code != 200:
        print(f"FAIL   create {r.status_code} {r.text[:300]}")
        return 1
    cid = r.json()["id"]
    r = client.patch(f"/campaigns/{cid}", json=it["patch"])
    if r.status_code != 200:
        print(f"FAIL   patch {r.status_code} {r.text[:300]}")
        return 1
    c = r.json()
    land = engine.check_landing(c)
    print(f"draft  {cid}  status={c['status']}  landing={land.get('status')} mentions_offer={land.get('mentions_offer')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
