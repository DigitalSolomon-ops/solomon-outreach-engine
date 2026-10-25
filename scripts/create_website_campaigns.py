"""Create one Website Launch outreach campaign per industry, as DRAFTS, through the engine API.

Reads the funnel's industry files (read-only) for labels, page headline and FAQ. Goes through the
engine API's own POST /campaigns and PATCH /campaigns/<id> (same validation The Creator uses), run
in-process against the live store. Idempotent: an industry whose campaign already exists is skipped.
Nothing launches; a draft never sends.

  python scripts/create_website_campaigns.py --funnel <presence-websites dir> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LANDING = "https://digitalsolomon.com/presence/websites/{slug}"
BOOKING = "https://api.digitalsolomon.com/widget/booking/B1TJ1xNheVMPH4iE3tqH?pw_industry={slug}"
GEOS = ["Gastonia, NC", "Charlotte, NC", "Concord, NC", "Huntersville, NC", "Rock Hill, SC", "Monroe, NC"]
QUERY = {"roofing": "roofing contractor", "hvac": "HVAC contractor", "plumbing": "plumber",
         "landscaping": "landscaping company", "cleaning": "house cleaning service", "med-spa": "med spa",
         "auto-detailing": "auto detailing", "pressure-washing": "pressure washing service",
         "electrician": "electrician", "home-remodeling": "home remodeling contractor"}
PRICE = "The website is $600 to build, then $97 a month."
PREVIEW_FACT = "I build a free preview of a new website for your business, so you can see it before deciding anything."
SEQUENCE = [
    {"step": 0, "day": 0, "angle": "First touch: the strongest true observation about their current website, then the preview question."},
    {"step": 1, "day": 3, "angle": "Short bump: the preview is there if they want a look; ask again in your own words."},
    {"step": 2, "day": 7, "angle": "One different true observation about their site; offer the preview again."},
    {"step": 3, "day": 14, "angle": "Polite close: last note about the preview, easy to say no, no pressure."},
]


def plan(funnel: Path) -> list[dict]:
    out = []
    for f in sorted((funnel / "industries").glob("*.json")):
        ind = json.loads(f.read_text(encoding="utf-8"))
        slug, plural = ind["slug"], ind["trade_plural"]
        page = ind.get("page") or {}
        out.append({
            "slug": slug,
            "create": {"name": f"Website Launch · {(plural[:1].upper() + plural[1:])} · Gaston/Charlotte",
                       "description": f"Website Launch for {plural} around Gaston County and Charlotte whose "
                                      "website is weak or broken. Email 1 offers a free preview; a yes gets it "
                                      "built and sent as Email 2.",
                       "landing_url": LANDING.format(slug=slug), "booking_url": BOOKING.format(slug=slug),
                       "offer_name": "Website Launch"},
            "patch": {
                "offer": {"name": "Website Launch", "facts": [PREVIEW_FACT, page.get("h1", "").strip(), PRICE],
                          "faq": [{"q": q["q"], "a": q["a"]} for q in (page.get("faqs") or [])[:4]]},
                "preview": {"enabled": True, "industry": slug, "price_fact": PRICE},
                "icp": {"cells": [{"segment": (plural[:1].upper() + plural[1:]), "geo": g, "query": QUERY.get(slug, plural)} for g in GEOS],
                        "site_filter": "weak_or_none"},
                "sequence": SEQUENCE,
            },
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--funnel", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    items = plan(Path(a.funnel))
    if a.dry_run:
        print(json.dumps(items, indent=1))
        return 0
    from fastapi.testclient import TestClient

    from leadgen.api import create_app
    from leadgen.engine import make_engine
    engine = make_engine()
    client = TestClient(create_app(lambda: engine))
    existing = {c["name"]: c for c in client.get("/campaigns").json()["campaigns"]}
    for it in items:
        name = it["create"]["name"]
        if name in existing:
            print(f"skip   {it['slug']:17} exists as {existing[name]['id']} ({existing[name]['status']})")
            continue
        r = client.post("/campaigns", json=it["create"])
        if r.status_code != 200:
            print(f"FAIL   {it['slug']:17} create {r.status_code} {r.text[:200]}")
            continue
        cid = r.json()["id"]
        r = client.patch(f"/campaigns/{cid}", json=it["patch"])
        if r.status_code != 200:
            print(f"FAIL   {it['slug']:17} patch {r.status_code} {r.text[:200]}")
            continue
        c = r.json()
        land = engine.check_landing(c)
        print(f"draft  {it['slug']:17} {cid}  status={c['status']}  landing={land.get('status')} "
              f"mentions_offer={land.get('mentions_offer')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
