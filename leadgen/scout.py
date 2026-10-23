"""Monthly provider scout (first health run of each month).

A few web searches through the rotation (at most 10 discovery units in total)
for providers with RECURRING free API tiers, per capability. Haiku extracts
candidates; code drops anything already in the registry. Finds are shown as
"proposed" (name, claimed free tier, source URL, verify note) and are never
added to the rotation automatically: adding one is a new adapter plus a key.
"""
from __future__ import annotations

import re

from .llm import LLMError
from .providers import ProviderError
from .rotation import NoProviderAvailable

QUERIES = {
    "verify_email": "email verification API free plan monthly credits",
    "enrich_people": "B2B email finder API free plan monthly searches",
    "discover_maps": "local business places search API free tier monthly",
    "scrape": "web scraping API free plan monthly credits",
    "discover_web": "web search API free tier monthly queries",
}

EXTRACT = """From these search results, list API providers that offer a RECURRING free tier (monthly or daily)
for the capability "{cap}". Return JSON only:
{{"candidates": [{{"name": "...", "claimed_free_tier": "...", "source_url": "..."}}]}}
Only include providers named in the results, with a source URL taken from the results. No guesses."""


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def scout(engine, max_units: int = 10) -> dict:
    known = {_norm(p["name"]) for p in engine.registry["providers"]}
    known |= {_norm(p["name"].split("_")[0]) for p in engine.registry["providers"]}
    proposals, units, notes = [], 0, []
    for cap, q in QUERIES.items():
        if units >= max_units:
            break
        try:
            cands, provider = engine.discover.call("search", q, "", 8, kinds=["web"])
        except (NoProviderAvailable, ProviderError) as exc:
            notes.append(f"{cap}: {exc}"[:200])
            continue
        units += 1
        if not cands:
            continue
        results = "\n".join(f"- {c.name} | {c.website}" for c in cands)
        try:
            out = engine.llm.json("haiku", EXTRACT.format(cap=cap), results, purpose="scout", max_tokens=500)
        except LLMError as exc:
            notes.append(f"{cap}: {exc}"[:200])
            continue
        urls = {c.website for c in cands}
        for x in out.get("candidates") or []:
            name = str(x.get("name", "")).strip()
            if not name or _norm(name) in known or x.get("source_url") not in urls:
                continue
            known.add(_norm(name))
            proposals.append({"name": name, "capability": cap, "claimed_free_tier": str(x.get("claimed_free_tier", ""))[:160],
                              "source_url": x["source_url"], "status": "proposed",
                              "note": "Verify the free tier on the vendor's own page before adding an adapter."})
    return {"proposals": proposals, "units_used": units, "notes": notes}
