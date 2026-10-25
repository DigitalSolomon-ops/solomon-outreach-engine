"""The fixed industry list (config/industries.yaml) and what picking one sets on a campaign.

One industry per campaign. Picking one sets the ICP cells (segment + search query per town).
For a Website Launch campaign (preview offer on) it also sets the preview industry, the
industry's landing and booking pages, its headline fact and its FAQ, so they can't drift apart.
"""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import yaml

from .config import ROOT


class IndustryError(ValueError):
    pass


_CACHE: dict | None = None


def catalog(path: str | os.PathLike | None = None) -> dict:
    global _CACHE
    if path is None and _CACHE is not None:
        return _CACHE
    with open(Path(path or ROOT / "config" / "industries.yaml"), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    slugs = [i["slug"] for i in data["industries"]]
    if len(slugs) != len(set(slugs)):
        raise IndustryError("industries.yaml: duplicate slug")
    for i in data["industries"]:
        if not all(i.get(k) for k in ("slug", "label", "query", "h1")):
            raise IndustryError(f"industries.yaml: {i.get('slug')!r} needs slug, label, query and h1")
    if path is None:
        _CACHE = data
    return data


def get(slug: str) -> dict:
    for i in catalog()["industries"]:
        if i["slug"] == slug:
            return i
    raise IndustryError(f"unknown industry {slug!r}; pick one from the list")


def public_list() -> dict:
    cat = catalog()
    return {"industries": [{"slug": i["slug"], "label": i["label"], "query": i["query"]} for i in cat["industries"]],
            "default_geos": cat["default_geos"], "area_label": cat["area_label"]}


def cells_for(ind: dict, geos: list[str] | None = None) -> list[dict]:
    return [{"segment": ind["label"], "geo": g, "query": ind["query"], "naics": "", "exclusions": []}
            for g in (geos or catalog()["default_geos"])]


def website_launch_fields(ind: dict, source: dict) -> dict:
    """The per-industry parts of a Website Launch campaign, taking everything else from source."""
    wl = catalog()["website_launch"]
    old = source.get("industry") or (source.get("preview") or {}).get("industry")
    old_h1 = get(old)["h1"] if old and any(i["slug"] == old for i in catalog()["industries"]) else None
    facts = list((source.get("offer") or {}).get("facts") or [])
    facts = [ind["h1"] if (old_h1 and f.strip() == old_h1) else f for f in facts]
    if ind["h1"] not in facts:
        facts.insert(min(1, len(facts)), ind["h1"])
    return {
        "landing_url": wl["landing_url"].format(slug=ind["slug"]),
        "booking_url": wl["booking_url"].format(slug=ind["slug"]),
        "offer": {**deepcopy(source.get("offer") or {}), "facts": facts,
                  "faq": [{"q": q["q"], "a": q["a"]} for q in ind.get("faq", [])]},
        "preview": {**deepcopy(source.get("preview") or {}), "industry": ind["slug"]},
    }


def clone_name(source: dict, ind: dict) -> str:
    """'Website Launch · Roofers · Gaston/Charlotte' -> 'Website Launch · Plumbers · Gaston/Charlotte'."""
    name = source["name"]
    old = source.get("industry") or (source.get("preview") or {}).get("industry")
    old_label = None
    if old and any(i["slug"] == old for i in catalog()["industries"]):
        old_label = get(old)["label"]
    elif source["icp"]["cells"]:
        old_label = source["icp"]["cells"][0]["segment"]
    if old_label and old_label in name:
        return name.replace(old_label, ind["label"], 1)
    return f"{name} · {ind['label']}"[:80]
