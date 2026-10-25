"""Campaigns: ICP cells, offer, landing page, sequence, and the launch state machine.

  draft --Launch--> waiting --preflight green--> testing --48h test passes--> active
                                                     +--test fails--> paused
  active/testing --auto-stop or operator--> paused --Resume--> active (or waiting if never tested)
  any --operator--> done

Nothing sends unless a campaign the operator launched is in testing or active,
its preflight passed, and the global kill switch is off.
"""
from __future__ import annotations

import re
import uuid
from copy import deepcopy
from urllib.parse import urlencode, urlparse

from . import strategies
from .config import load_icp_file
from .store import utcnow

STATUSES = ("draft", "waiting", "testing", "active", "paused", "done")
SENDING_STATUSES = ("testing", "active")
SEED_ID = "ai-readiness-audit-gaston-charlotte"
SEED_NAME = "AI Readiness Audit · Gaston/Charlotte"
TIERS = ("haiku", "sonnet")
# Who a campaign targets is its strategy (icp.strategy + icp.strategy_params, the registry in
# config/strategies.yaml). icp.site_filter is the pre-registry field: still accepted as an alias
# and kept in step (any -> any_site, weak -> weak_site, weak_or_none -> weak_or_no_site,
# new_listing -> new_business).
SITE_FILTERS = tuple(strategies.FROM_SITE_FILTER)
DEFAULT_MAX_REVIEWS = 5


class CampaignError(ValueError):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def cell_id(segment: str, geo: str) -> str:
    return f"{slug(segment)}@{slug(geo)}"


def default_settings(settings: dict) -> dict:
    return {
        "daily_cap": 30,
        "writer_tier": "haiku",
        "writer_tier_manual": False,
        "positive_reply_target": settings["writing"]["escalate_positive_rate_below"],
        "ai_reply_cap": settings["replies"]["ai_reply_cap"],
        "test_batch_size": settings["send"]["test_batch_size"],
        "recontact_after_days": settings["discovery"]["recontact_after_days"],
        # Preflight writes 5 sample emails; this many must pass the validator and critic.
        "samples_required": 5,
    }


def _is_url(u: str) -> bool:
    p = urlparse(u or "")
    return p.scheme in ("http", "https") and bool(p.netloc)


def validate(c: dict) -> dict:
    """Fail on save, with the field name, rather than mid-run."""
    problems = []
    if not (c.get("name") or "").strip():
        problems.append("name is required")
    if not _is_url(c.get("landing_url", "")):
        problems.append("landing_url is required and must be an http(s) URL")
    if c.get("booking_url") and not _is_url(c["booking_url"]):
        problems.append("booking_url must be an http(s) URL")
    if c.get("status", "draft") not in STATUSES:
        problems.append(f"status must be one of {STATUSES}")
    offer = c.get("offer") or {}
    if not offer.get("name"):
        problems.append("offer.name is required")
    if not isinstance(offer.get("facts", []), list) or not all(isinstance(f, str) for f in offer.get("facts", [])):
        problems.append("offer.facts must be a list of sentences")
    for q in offer.get("faq", []):
        if not isinstance(q, dict) or not q.get("q") or not q.get("a"):
            problems.append("offer.faq items need q and a")
            break
    for cell in (c.get("icp") or {}).get("cells", []):
        if not cell.get("segment") or not cell.get("geo") or not cell.get("query"):
            problems.append("each ICP cell needs segment, geo and query")
            break
    site_filter = (c.get("icp") or {}).get("site_filter", "any")
    pv = c.get("preview") or {}
    req = (c.get("settings") or {}).get("samples_required", 5)
    if not isinstance(req, int) or isinstance(req, bool) or not 1 <= req <= 5:
        problems.append("settings.samples_required must be a whole number from 1 to 5")
    if site_filter not in SITE_FILTERS:
        problems.append(f"icp.site_filter must be one of {SITE_FILTERS}")
    else:
        problems.extend(strategies.validate_campaign(c))
    if pv.get("price_fact") and pv["price_fact"].strip() not in [f.strip() for f in offer.get("facts", [])]:
        problems.append("preview.price_fact must be one of offer.facts, word for word")
    if pv.get("enabled") and not pv.get("price_fact"):
        problems.append("preview.price_fact is required when the preview offer is enabled")
    seq = c.get("sequence") or []
    if seq and [s.get("step") for s in seq] != list(range(len(seq))):
        problems.append("sequence steps must be numbered 0..n-1 in order")
    if any(not isinstance(s.get("day"), int) or s["day"] < 0 for s in seq):
        problems.append("sequence days must be non-negative integers")
    tier = (c.get("settings") or {}).get("writer_tier", "haiku")
    if tier not in TIERS:
        problems.append(f"settings.writer_tier must be one of {TIERS}")
    if problems:
        raise CampaignError("; ".join(problems))
    return c


def new_campaign(settings: dict, *, name: str, description: str, landing_url: str,
                 booking_url: str = "", offer: dict | None = None, cells: list[dict] | None = None,
                 private: bool = False, cid: str | None = None) -> dict:
    now = utcnow().isoformat()
    c = {
        "id": cid or f"{slug(name)[:30]}-{uuid.uuid4().hex[:6]}",
        "name": name.strip(), "description": description.strip(), "status": "draft",
        "landing_url": landing_url.strip(), "booking_url": (booking_url or "").strip(),
        "offer": {"name": "", "facts": [], "faq": [], "sender_name": "Marcus Barber",
                  "sender_company": "Digital Solomon", **(offer or {})},
        "icp": {"cells": [_cell(x) for x in (cells or [])],
                "exclude_domains": list(settings["discovery"]["exclude_domains"]),
                "exclude_name_keywords": list(settings["discovery"]["exclude_name_keywords"]),
                "prefer_kinds": list(settings["discovery"]["prefer_kinds"]),
                # the lead signal (config/strategies.yaml); site_filter is the legacy alias
                "strategy": "any_site", "strategy_params": {}, "site_filter": "any"},
        "sequence": deepcopy(settings["sequence_default"]),
        "settings": default_settings(settings),
        # Preview offer: Email 1 asks "Want to see the preview I built for you?"; a yes
        # (reply class preview_yes) requests one from the funnel and sends Email 2.
        # price_fact: the one offer fact (verbatim) that states the price, used in Email 2.
        "preview": {"enabled": False, "industry": "", "price_fact": ""},
        "private": private, "cells_state": {}, "created_at": now, "updated_at": now,
    }
    return c


def _cell(x: dict) -> dict:
    return {"id": x.get("id") or cell_id(x["segment"], x["geo"]), "segment": x["segment"],
            "geo": x["geo"], "query": x["query"], "naics": x.get("naics", ""),
            "exclusions": x.get("exclusions", [])}


def seed_from_icp(settings: dict) -> dict:
    """Migrate the icp.yaml lane into the first campaign, status draft."""
    icp = load_icp_file()
    lane = icp["lanes"]["ai-readiness-audit"]
    c = new_campaign(
        settings, cid=SEED_ID, name=SEED_NAME,
        description=("Free AI Readiness Audit for local service businesses in Gaston County "
                     "and the Charlotte metro."),
        landing_url="https://digitalsolomon.com/audit",
        booking_url="https://digitalsolomon.com/contact",
        offer={"name": lane["offer"]["name"], "facts": list(lane["offer"]["facts"]),
               "sender_name": lane["offer"].get("sender_name", "Marcus Barber"),
               "sender_company": lane["offer"].get("sender_company", "Digital Solomon")},
        cells=[{"segment": s["name"], "geo": g, "query": s["query"]}
               for s in lane["segments"] for g in lane["geos"]])
    c["icp"]["exclude_domains"] = sorted(set(c["icp"]["exclude_domains"])
                                         | {d.lower() for d in lane.get("exclude_domains", [])})
    c["icp"]["exclude_name_keywords"] = lane.get("exclude_name_keywords", [])
    c["seeded_from"] = "config/icp.yaml"
    return validate(c)


def ensure_seed(ledger, settings: dict) -> dict:
    existing = ledger.campaign(SEED_ID)
    if existing:
        return existing
    c = seed_from_icp(settings)
    ledger.save_campaign(c["id"], c)
    ledger.event("campaign_created", f"Seed campaign \"{c['name']}\" created as a draft.",
                 why="Migrated from config/icp.yaml.", campaign_id=c["id"])
    return c


def offers_preview(c: dict) -> bool:
    """Preview campaigns: Email 1 asks "Want to see the preview I built for you?", plain text, no links."""
    return bool((c.get("preview") or {}).get("enabled"))


def campaign_link(c: dict, domain: str, step: int) -> str:
    base = c["landing_url"]
    sep = "&" if "?" in base else "?"
    return base + sep + urlencode({"utm_source": "leadgen", "utm_medium": "email",
                                   "utm_campaign": c["id"], "utm_content": domain,
                                   "utm_term": f"step{step}"})


EDITABLE = {"name", "description", "landing_url", "booking_url", "offer", "icp", "sequence",
            "settings", "preview"}


def apply_patch(c: dict, patch: dict) -> dict:
    bad = set(patch) - EDITABLE
    if bad:
        raise CampaignError(f"these fields can't be edited directly: {sorted(bad)}")
    nxt = deepcopy(c)
    for k, v in patch.items():
        if k in ("settings", "offer", "icp", "preview") and isinstance(v, dict):
            nxt[k] = {**nxt.get(k, {}), **v}
        else:
            nxt[k] = v
    if "icp" in patch:
        icp_patch = patch["icp"] if isinstance(patch["icp"], dict) else {}
        if "strategy" in icp_patch and "site_filter" not in icp_patch:
            # a new strategy pick: site_filter follows it rather than overriding it
            nxt["icp"]["site_filter"] = strategies.TO_SITE_FILTER.get(icp_patch["strategy"], "any")
        if "strategy" in icp_patch and icp_patch["strategy"] != strategies.strategy_id(c) \
                and "strategy_params" not in icp_patch:
            nxt["icp"]["strategy_params"] = {}   # another strategy's settings don't carry over
        if "strategy_params" in icp_patch and isinstance(icp_patch["strategy_params"], dict):
            nxt["icp"]["strategy_params"] = dict(icp_patch["strategy_params"])
            for name, legacy in strategies.LEGACY_PARAMS.get(strategies.strategy_id(nxt), {}).items():
                if name in icp_patch["strategy_params"] and legacy not in icp_patch:
                    nxt["icp"][legacy] = icp_patch["strategy_params"][name]
        if nxt["icp"].get("site_filter", "any") not in SITE_FILTERS:
            raise CampaignError(f"icp.site_filter must be one of {SITE_FILTERS}")
        raw = nxt["icp"].get("strategy")
        if raw and raw not in strategies.load()["by_id"]:
            raise CampaignError(f"icp.strategy {raw!r} is not a known strategy")
        nxt["icp"] = strategies.normalize_icp(nxt["icp"])
        nxt["icp"]["cells"] = [_cell(x) for x in nxt["icp"].get("cells", [])]
    if "settings" in patch and "writer_tier" in patch["settings"]:
        nxt["settings"]["writer_tier_manual"] = True
    return validate(nxt)
