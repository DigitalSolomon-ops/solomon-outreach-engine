"""The strategist: one schema-validated JSON plan per campaign.

Compiler over generative: Sonnet authors the qualitative parts (ICP cells,
offer facts read off the landing page, FAQ, source order, sequence angles,
risks, connections worth adding) as JSON; code validates it and rejects any
unknown provider or field. Every NUMBER in the plan (reachable leads, cost,
days to full volume) is computed by code from Census counts, a small live
probe, the vault inventory and the price table, never by the model.

Recommendations are information, never a gate: a campaign runs on whatever is connected.
"""
from __future__ import annotations

import logging
import math
import re

import jsonschema

from .capabilities import STAGE_CAPS, vault_slot
from .enrich import page_text
from .extract import contact_page, read_site
from .llm import LLMError
from .providers import ProviderError
from .rotation import NoProviderAvailable
from .store import utcnow

log = logging.getLogger(__name__)

PLAN_CAPS = ("discover_maps", "discover_web", "scrape", "verify_email", "enrich_people")

MODEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["icp_cells", "offer", "source_plan", "sequence", "risks", "recommended_connections"],
    "properties": {
        "icp_cells": {"type": "array", "minItems": 1, "maxItems": 60, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["segment", "geo", "query"],
            "properties": {"segment": {"type": "string", "minLength": 2},
                           "geo": {"type": "string", "minLength": 2},
                           "query": {"type": "string", "minLength": 2},
                           "naics": {"type": "string", "pattern": "^[0-9]{2,6}$"},
                           "exclusions": {"type": "array", "items": {"type": "string"}}}}},
        "offer": {"type": "object", "additionalProperties": False, "required": ["name", "facts"],
                  "properties": {"name": {"type": "string", "minLength": 2},
                                 "facts": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "string"}},
                                 "faq": {"type": "array", "items": {
                                     "type": "object", "additionalProperties": False, "required": ["q", "a"],
                                     "properties": {"q": {"type": "string"}, "a": {"type": "string"}}}}}},
        "source_plan": {"type": "object", "additionalProperties": False,
                        "properties": {c: {"type": "array", "items": {"type": "string"}} for c in PLAN_CAPS}},
        "sequence": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False, "required": ["step", "day", "angle"],
            "properties": {"step": {"type": "integer", "minimum": 0}, "day": {"type": "integer", "minimum": 0},
                           "angle": {"type": "string"}}}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "recommended_connections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "why"],
            "properties": {"name": {"type": "string"}, "why": {"type": "string"}}}},
    },
}

# Rough token sizes per call, used only to estimate monthly AI cost in code.
TOKENS = {"brief": (1500, 300), "write": (1900, 350), "critic": (1600, 120), "classify": (600, 80),
          "reply": (1500, 250), "reply_critic": (1200, 100)}
PEOPLE_CEILING_PER_MONTH = 125


class PlanError(ValueError):
    pass


def validate_model_plan(plan: dict, registry: dict) -> list[str]:
    """Schema, then registry checks: every provider named must exist and not be 'skip'."""
    try:
        jsonschema.validate(plan, MODEL_SCHEMA)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        return [f"schema: {where or '(root)'}: {exc.message}"]
    known = {p["name"]: p for p in registry["providers"]}
    problems = []
    for cap, names in plan["source_plan"].items():
        for n in names:
            p = known.get(n)
            if not p:
                problems.append(f"source_plan.{cap}: unknown provider {n!r}")
            elif p["status"] == "skip":
                problems.append(f"source_plan.{cap}: {n} is marked skip")
            elif cap not in p.get("capabilities", []):
                problems.append(f"source_plan.{cap}: {n} does not provide {cap}")
    for r in plan["recommended_connections"]:
        p = known.get(r["name"])
        if not p:
            problems.append(f"recommended_connections: unknown provider {r['name']!r}")
        elif p["status"] == "skip" or not p.get("secret"):
            problems.append(f"recommended_connections: {r['name']} can't be connected with a key")
    steps = [s["step"] for s in plan["sequence"]]
    if steps != list(range(len(steps))):
        problems.append("sequence steps must be numbered 0..n-1")
    if [s["day"] for s in plan["sequence"]] != sorted(s["day"] for s in plan["sequence"]):
        problems.append("sequence days must increase")
    if plan["sequence"] and plan["sequence"][0]["day"] != 0:
        problems.append("sequence step 0 must be day 0")
    return problems


SYSTEM = """You are a cold email strategist for Digital Solomon, a small studio. Plan ONE campaign.
You get: the operator's description of who, where and what is offered; the landing page text; and the
providers that are CONNECTED (usable now) and NOT CONNECTED, per capability.
Rules:
- icp_cells: segment x geo pairs inside what the operator described. Do not add segments or places they did not ask for.
  query = what to type into a maps search. naics = the 2017 NAICS code (2-6 digits) for the segment, if you know it.
- offer.facts: short sentences that are TRUE according to the landing page or the description. No numbers that are not there.
  offer.faq: likely prospect questions answered only from those facts.
- source_plan: for each capability, order the CONNECTED provider names you would use. Use only names from the lists.
- sequence: 4 steps on days 0, 3, 7, 14 unless the description asks otherwise; give each a distinct angle.
- risks: plain sentences.
- recommended_connections: NOT CONNECTED providers worth adding, with why. Use only names from the lists.
- Do NOT output any numbers for volume, cost or time; the system computes those.
Return JSON only with keys: icp_cells, offer, source_plan, sequence, risks, recommended_connections."""


class Strategist:
    def __init__(self, settings, ledger, llm, registry, inventory, *, discover, scrape, sizing=None,
                 models=None):
        self.settings = settings
        self.ledger = ledger
        self.llm = llm
        self.registry = registry
        self.inv = inventory
        self.discover = discover
        self.scrape = scrape
        self.sizing = sizing          # Rotation over census adapters, or None
        self.models = models or {}

    # ------------------------------------------------------------------ inputs
    def landing(self, url: str) -> dict:
        try:
            page, _ = self.scrape.call("fetch", url)
        except (ProviderError, NoProviderAvailable) as exc:
            return {"ok": False, "error": str(exc)[:200], "text": ""}
        text = page_text([page])
        return {"ok": bool(text.strip()), "text": text[:12000], "url": url}

    def _inventory_text(self) -> str:
        lines = []
        for cap in PLAN_CAPS:
            on = [p["name"] for p in self.inv["providers"] if p["connected"] and cap in p["capabilities"]]
            off = [p["name"] for p in self.inv["providers"] if not p["connected"] and cap in p["capabilities"]]
            lines.append(f"{cap}: CONNECTED {on or '(none)'}; NOT CONNECTED {off or '(none)'}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ probe + sizing
    def probe(self, cells: list[dict], max_queries: int = 3, max_reads: int = 10) -> dict:
        """At most 3 discovery queries and 10 site reads: what share of sites publish an email."""
        queries = reads = with_email = 0
        notes = []
        for cell in cells[:max_queries]:
            try:
                cands, provider = self.discover.call("search", cell["query"], cell["geo"], 10,
                                                     kinds=self.settings["discovery"]["prefer_kinds"])
            except NoProviderAvailable as exc:
                notes.append(f"discovery: {exc}")
                break
            except ProviderError as exc:
                notes.append(f"discovery failed for {cell['segment']} in {cell['geo']}: {exc}"[:200])
                continue
            queries += 1
            for cand in cands:
                if reads >= max_reads:
                    break
                if not cand.website:
                    continue
                try:
                    home, _ = self.scrape.call("fetch", _homepage(cand.website))
                except (ProviderError, NoProviderAvailable):
                    continue
                reads += 1
                pages = [home]
                cp = contact_page(home)
                if cp and reads < max_reads:
                    try:
                        page, _ = self.scrape.call("fetch", cp)
                        pages.append(page)
                        reads += 1
                    except (ProviderError, NoProviderAvailable):
                        pass
                site = read_site(pages)
                if site.best_email and not site.harvest_notice:
                    with_email += 1
            if reads >= max_reads:
                break
        sites = max(1, reads)
        return {"queries": queries, "site_reads": reads, "sites_with_email": with_email,
                "email_findable_rate": round(with_email / sites, 3) if reads else None, "notes": notes}

    def size(self, cells: list[dict]) -> dict:
        from .providers.sizing import city_to_county
        if not self.sizing:
            return {"available": False, "note": "No Census key connected; sized from the probe alone."}
        seen, missing = [], []
        for cell in cells:
            geo = city_to_county(cell["geo"])
            if not cell.get("naics") or not geo:
                missing.append(f"{cell['segment']} in {cell['geo']}")
                continue
            if (cell["naics"], geo) not in seen:
                seen.append((cell["naics"], geo))
        from concurrent.futures import ThreadPoolExecutor

        def one(key):
            naics, (state, county) = key
            try:
                return self.sizing.call("establishments", naics, state, county)[0] or 0
            except (ProviderError, NoProviderAvailable):
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one, seen[:60]))
        if seen and all(r is None for r in results):
            return {"available": False, "note": "Census lookups failed; sized from the probe alone."}
        counts = sum(r for r in results if r)
        failed = sum(1 for r in results if r is None)
        if failed:
            missing.append(f"{failed} Census lookup(s) failed")
        return {"available": True, "establishments": counts, "lookups": len(results),
                "unsized_cells": missing[:20],
                "note": "Census County Business Patterns establishment counts (latest vintage) per segment x county."}

    # ------------------------------------------------------------------ numbers (code only)
    def numbers(self, campaign: dict, sizing: dict, probe: dict, mailboxes: int) -> dict:
        s = self.settings
        daily_cap = int(campaign["settings"]["daily_cap"])
        per_mb_max = s["send"]["warmup_max"]
        full = min(daily_cap, per_mb_max * mailboxes) if mailboxes else 0
        if mailboxes:
            per_mb_needed = math.ceil(full / mailboxes)
            days = max(0, math.ceil((per_mb_needed - s["send"]["warmup_start"]) / s["send"]["warmup_ramp_per_day"]))
        else:
            days = None
        rate = probe.get("email_findable_rate")
        reachable = None
        if sizing.get("available") and rate is not None:
            reachable = int(sizing["establishments"] * rate)
        # With no mailbox yet, estimate at the campaign's own daily cap (what it will run at).
        first_touches_month = (full or daily_cap) * 21
        steps = len(campaign["sequence"])
        cost = 0.0
        if self.models:
            def usd(tier, kind, n):
                i, o = TOKENS[kind]
                m = self.models[tier]
                return n * (i / 1e6 * m["input_per_mtok"] + o / 1e6 * m["output_per_mtok"])
            tier = campaign["settings"].get("writer_tier", "haiku")
            emails = first_touches_month * steps
            replies = first_touches_month * 0.03
            cost = (usd("haiku", "brief", first_touches_month) + usd(tier, "write", emails)
                    + usd("haiku", "critic", emails) + usd("sonnet", "write", emails * 0.15)
                    + usd("haiku", "classify", replies) + usd("haiku", "reply", replies * 1.5)
                    + usd("haiku", "reply_critic", replies * 1.5))
        return {
            "reachable_leads_estimate": reachable,
            "reachable_basis": (f"{sizing.get('establishments')} establishments x {rate:.0%} publish an email"
                                if reachable is not None else sizing.get("note")),
            "people_data_ceiling_per_month": PEOPLE_CEILING_PER_MONTH,
            "full_volume_per_day": full,
            "days_to_full_volume": days,
            "days_basis": (f"{mailboxes} mailbox(es) ramping {s['send']['warmup_start']}/day +"
                           f"{s['send']['warmup_ramp_per_day']}/day to {per_mb_max}"
                           if mailboxes else "No sending mailbox yet: the ramp starts when one is added."),
            "first_touches_per_month": first_touches_month,
            "est_monthly_ai_cost_usd": round(cost, 2),
            "cost_basis": f"{first_touches_month} first touches a month x {steps} steps, writer {campaign['settings'].get('writer_tier', 'haiku')}",
            "est_monthly_provider_cost_usd": 0.0,
            "provider_cost_basis": "Free allowances only (paid cap $0); a stage waits for resets when spent.",
        }

    # ------------------------------------------------------------------ the plan
    def plan(self, campaign: dict, mailboxes: int) -> dict:
        land = self.landing(campaign["landing_url"])
        user = "\n\n".join([
            f"Operator description:\n{campaign.get('description') or campaign['name']}",
            f"Current offer name: {campaign['offer'].get('name') or '(none)'}",
            "Current offer facts:\n" + "\n".join(f"- {f}" for f in campaign["offer"].get("facts", [])),
            "Current ICP cells: " + (", ".join(f"{c['segment']} in {c['geo']}" for c in campaign["icp"]["cells"][:60]) or "(none)"),
            "Landing page text:\n" + (land["text"] or f"(could not read the landing page: {land.get('error')})"),
            "Providers:\n" + self._inventory_text(),
        ])
        feedback, raw, problems = "", None, []
        for _ in range(2):
            try:
                raw = self.llm.json("sonnet", SYSTEM, user + feedback, purpose="plan",
                                    campaign_id=campaign["id"], max_tokens=8000, schema=MODEL_SCHEMA)
            except LLMError as exc:
                problems = [str(exc)]
                feedback = f"\n\nYour last answer failed: {exc}. Return JSON only."
                continue
            problems = validate_model_plan(raw, self.registry)
            if not problems:
                break
            feedback = "\n\nYour last plan was rejected: " + "; ".join(problems[:10]) + ". Fix it."
        if problems:
            raise PlanError("; ".join(problems[:10]))
        cells = campaign["icp"]["cells"] or raw["icp_cells"]
        if campaign["icp"]["cells"]:
            # keep the operator's cells; borrow NAICS codes the model proposed for the same segment
            naics = {c["segment"].lower(): c.get("naics") for c in raw["icp_cells"] if c.get("naics")}
            cells = [{**c, "naics": c.get("naics") or naics.get(c["segment"].lower(), "")} for c in cells]
        sizing = self.size(cells)
        probe = self.probe(cells)
        by_name = {p["name"]: p for p in self.registry["providers"]}
        recs = []
        for r in raw["recommended_connections"]:
            p = by_name[r["name"]]
            if any(x["name"] == r["name"] and x["connected"] for x in self.inv["providers"]):
                continue
            recs.append({"name": r["name"], "why": r["why"], "free_tier": p.get("free"),
                         "vault_slot": vault_slot(p["secret"]), "docs": p.get("docs")})
        plan = {
            "at": utcnow().isoformat(), "model": "sonnet",
            "icp_cells": raw["icp_cells"], "offer": raw["offer"], "source_plan": raw["source_plan"],
            "sequence": raw["sequence"], "risks": raw["risks"] + (
                [f"The landing page could not be read: {land.get('error')}"] if not land["ok"] else []),
            "recommended_connections": recs, "landing": {"ok": land["ok"], "url": campaign["landing_url"]},
            "sizing": sizing, "probe": probe,
            **self.numbers(campaign, sizing, probe, mailboxes),
        }
        return plan


def _homepage(url: str) -> str:
    from urllib.parse import urlparse
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def apply_plan(campaign: dict, plan: dict) -> dict:
    """Fill what the operator hasn't set yet. Never overwrites their cells, facts or FAQ."""
    from .campaigns import _cell
    c = dict(campaign)
    if not c["icp"]["cells"]:
        c["icp"] = {**c["icp"], "cells": [_cell(x) for x in plan["icp_cells"]]}
    else:
        naics = {x["segment"].lower(): x.get("naics") for x in plan["icp_cells"] if x.get("naics")}
        c["icp"] = {**c["icp"], "cells": [{**x, "naics": x.get("naics") or naics.get(x["segment"].lower(), "")}
                                          for x in c["icp"]["cells"]]}
    offer = dict(c["offer"])
    if not offer.get("name"):
        offer["name"] = plan["offer"]["name"]
    if not offer.get("facts"):
        offer["facts"] = plan["offer"]["facts"]
    if not offer.get("faq"):
        offer["faq"] = plan["offer"].get("faq", [])
    offer["proposed_facts"] = plan["offer"]["facts"]
    offer["proposed_faq"] = plan["offer"].get("faq", [])
    c["offer"] = offer
    if not c.get("sequence_edited"):
        days = {s["step"]: s["day"] for s in c["sequence"]}
        c["sequence"] = [{"step": s["step"], "day": days.get(s["step"], s["day"]), "angle": s["angle"]}
                         for s in plan["sequence"]]
    c["plan"] = plan
    return c


STAGE_FOR_CAP = {cap: stage for stage, caps in STAGE_CAPS.items() for cap in caps}
_ = re  # noqa: re kept for future query cleaning
