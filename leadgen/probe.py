"""strategy-probe: measure one strategy's find rates live, without a campaign and without sending.

  python -m leadgen strategy-probe <id> --geo "Gastonia, NC" [--geo ...] --n 50

Runs the strategy's real passes (find, keep or drop, own-site read, email, one verification)
against a throwaway in-memory campaign. Nothing is written to leadgen_leads or leadgen_campaigns,
no brief or owner-name model call is made, and nothing is drafted or sent. Provider usage IS
counted in leadgen_usage like any run, so the free caps stay true. Writes
docs/strategies/<id>-probe.md with candidates, kept, email found, verified and the rate at each
step.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from pathlib import Path

from . import campaigns as camp
from . import industries as inds
from . import strategies
from .llm import BudgetStop
from .prospect import Prospector
from .providers import ProviderError
from .rotation import NoProviderAvailable

log = logging.getLogger(__name__)

DROPPED = {"site_not_weak", "site_unassessed", "booking_found", "excluded", "strategy_dropped"}
NO_EMAIL = {"no_email", "scrape_failed", "harvest_notice"}
PER_SEARCH = 20


class ProbeLedger:
    """The real ledger for reads and provider usage; leads stay in memory."""

    def __init__(self, ledger):
        self._real = ledger
        self.leads_mem: dict[str, dict] = {}

    def __getattr__(self, name):
        return getattr(self._real, name)

    def lead(self, campaign_id, domain):
        return self.leads_mem.get(domain)

    def save_lead(self, campaign_id, domain, doc):
        self.leads_mem[domain] = {**self.leads_mem.get(domain, {}), **doc}

    def leads(self, campaign_id, *statuses):
        return [l for l in self.leads_mem.values() if not statuses or l.get("status") in statuses]

    def event(self, *a, **k):
        return None


def probe_campaign(settings: dict, sid: str, geos: list[str], industry: str | None, segment: str | None,
                   params: dict | None) -> dict:
    s = strategies.entry(sid)
    if s["uses_industry_list"]:
        ind = inds.get(industry or "auto-detailing")
        cells = inds.cells_for(ind, geos)
    else:
        seg = segment or s["label"]
        cells = [{"segment": seg, "geo": g, "query": seg} for g in geos]
    c = camp.new_campaign(settings, name=f"Probe {sid}", description=s["description"],
                          landing_url="https://digitalsolomon.com/", offer={"name": "Probe", "facts": ["Probe."]},
                          cells=cells, cid=f"probe-{sid}")
    c["icp"] = strategies.normalize_icp({**c["icp"], "strategy": sid, "site_filter": strategies.TO_SITE_FILTER.get(sid, "any"),
                                         "strategy_params": dict(params or {})})
    if s["needs_preview"] == "required":
        c["preview"] = {"enabled": True, "industry": industry or "auto-detailing", "price_fact": "Probe."}
    return c


class Probe:
    def __init__(self, engine, sid: str, geos: list[str], n: int = 50, industry: str | None = None,
                 segment: str | None = None, params: dict | None = None):
        self.e = engine
        self.sid = sid
        self.geos = geos
        self.n = n
        self.c = probe_campaign(engine.settings, sid, geos, industry, segment, params)
        self.p = Prospector(engine)
        self.p.ledger = ProbeLedger(engine.ledger)
        self.p.probe = True
        self.strat = self.p.strategy(self.c)
        self.outcomes = Counter()
        self.reasons = Counter()
        self.candidates = 0
        self.stopped = ""
        self.errors: list[str] = []

    def full(self) -> bool:
        return self.candidates >= self.n

    def one(self, fn, *args):
        try:
            outcome, _ = fn(*args)
        except (NoProviderAvailable, BudgetStop) as exc:
            self.stopped = str(exc)[:300]
            return False
        except ProviderError as exc:
            self.errors.append(str(exc)[:300])
            outcome = "error"
        self.outcomes[outcome] += 1
        return True

    def run(self) -> dict:
        c, p, strat = self.c, self.p, self.strat
        self.e.report = {"errors": []}
        for name in strat.passes:
            if self.full() or self.stopped:
                break
            getattr(self, f"_{name}")(c, p, strat)
        return self.report()

    def _site(self, c, p, strat):
        # One search per cell: maps searches return the same top results every time, so a second
        # round only spends credits. 20 per search (Google's page size) to reach n across the towns.
        seen = set()
        for cell in c["icp"]["cells"]:
            if self.full() or self.stopped:
                break
            try:
                cands, provider = strat.find(p, c, cell, max(PER_SEARCH, self.e.settings["discovery"]["candidates_per_query"]))
            except NoProviderAvailable as exc:
                self.stopped = str(exc)[:300]
                break
            except ProviderError as exc:
                self.errors.append(str(exc)[:300])
                continue
            for cand in cands:
                if self.full():
                    break
                if not cand.domain or cand.domain in seen:
                    continue
                seen.add(cand.domain)
                self.candidates += 1
                why = p.excluded(c, cand)
                if why:
                    self.outcomes["excluded"] += 1
                    self.reasons[why] += 1
                    continue
                if p.drop_listing(c, strat, cand, cell, provider):
                    self.outcomes["strategy_dropped"] += 1
                    self.reasons[(p.ledger.lead(c["id"], cand.domain) or {}).get("reason", "provider error")] += 1
                    continue
                if not self.one(p.qualify, c, cand, cell, provider, strat):
                    return
                reason = (p.ledger.lead(c["id"], cand.domain) or {}).get("reason")
                if reason:
                    self.reasons[reason] += 1

    def _nosite(self, c, p, strat):
        for cell in c["icp"]["cells"]:
            if self.full() or self.stopped:
                break
            for listing in p.nosite_listings(c, cell, 20):
                if self.full():
                    break
                self.candidates += 1
                if not self.one(p.qualify_nosite, c, listing, cell):
                    return

    def _source(self, c, p, strat):
        for cell in c["icp"]["cells"]:
            if self.full() or self.stopped:
                break
            try:
                items = p.source_items(c, strat, cell, self.n - self.candidates)
            except NoProviderAvailable as exc:
                self.stopped = str(exc)[:300]
                break
            except ProviderError as exc:
                self.errors.append(str(exc)[:300])
                continue
            for item in items:
                if self.full():
                    break
                self.candidates += 1
                if not self.one(strat.qualify_source, p, c, item, cell):
                    return
                reason = (p.ledger.lead(c["id"], item.key) or {}).get("reason")
                if reason:
                    self.reasons[reason] += 1

    def report(self) -> dict:
        o = self.outcomes
        dropped = sum(o[k] for k in DROPPED)
        kept = self.candidates - dropped
        email_found = kept - sum(o[k] for k in NO_EMAIL) - o["error"]
        verified = o["verified"]
        units = {}
        nosite = getattr(self.p, "_nosite", None)     # the no-website pass's own maps rotation
        for rot in [self.e.discover, self.e.scrape, self.e.reader, self.e.verify, self.e.people,
                    *getattr(self.e, "_signals", {}).values(), getattr(nosite, "maps", None)]:
            if rot:
                for name, u in rot.used.items():
                    units[f"{rot.stage}/{name}"] = units.get(f"{rot.stage}/{name}", 0) + u
        sources = Counter((l.get("email_source") or "").split(":")[0] for l in self.p.ledger.leads_mem.values()
                          if l.get("email"))
        extra = {}
        if hasattr(self.strat, "counts"):
            extra["source_counts"] = self.strat.counts
        if hasattr(self.strat, "seen"):
            extra["source_filter"] = self.strat.seen
        if hasattr(self.strat, "answers"):
            extra["ai_answers"] = [{k: a[k] for k in ("query", "date", "provider", "names")}
                                   for a in self.strat.answers.values()]
        return {"strategy": self.sid, "label": self.strat.entry["label"], "date": date.today().isoformat(),
                "geos": self.geos, "n": self.n, "segment": self.c["icp"]["cells"][0]["segment"],
                "candidates": self.candidates, "kept": kept, "email_found": email_found, "verified": verified,
                "rates": {"kept": _rate(kept, self.candidates), "email_found": _rate(email_found, kept),
                          "verified": _rate(verified, email_found),
                          "verified_per_candidate": _rate(verified, self.candidates)},
                "outcomes": dict(o), "reasons": dict(self.reasons.most_common(15)),
                "email_sources": dict(sources), "enrichment_calls": self.p.enrichment_calls,
                "provider_units": units, "stopped": self.stopped, "errors": self.errors[:10], **extra}


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def to_markdown(r: dict) -> str:
    rt = r["rates"]
    lines = [f"# Strategy probe: {r['strategy']} ({r['label']})", "",
             f"Live run {r['date']}, geos {', '.join(r['geos'])}, segment \"{r['segment']}\", n = {r['n']}. "
             "No campaign, no drafts, nothing sent.", "",
             "| Step | Count | Rate |", "|---|---|---|",
             f"| Candidates | {r['candidates']} | |",
             f"| Kept by the strategy | {r['kept']} | {_pct(rt['kept'])} of candidates |",
             f"| Email found | {r['email_found']} | {_pct(rt['email_found'])} of kept |",
             f"| Verified (sendable) | {r['verified']} | {_pct(rt['verified'])} of found, "
             f"{_pct(rt['verified_per_candidate'])} of candidates |", "",
             f"Enrichment (people-data) calls: {r['enrichment_calls']}.", ""]
    if r["reasons"]:
        lines += ["## Why candidates went no further", "", "| Reason | Count |", "|---|---|",
                  *[f"| {k} | {v} |" for k, v in r["reasons"].items()], ""]
    lines += ["## Outcomes", "", "| Outcome | Count |", "|---|---|",
              *[f"| {k} | {v} |" for k, v in sorted(r["outcomes"].items())], ""]
    if r["email_sources"]:
        lines += ["## Where the emails came from", "",
                  *[f"- {k or 'unknown'}: {v}" for k, v in r["email_sources"].items()], ""]
    units = [f"- {k}: {v}" for k, v in sorted(r["provider_units"].items())] or ["- none"]
    lines += ["## Provider units used", "", *units, ""]
    for key, title in (("source_counts", "Source counts (read before the ingest)"),
                       ("source_filter", "Source filter"), ("ai_answers", "AI answers used as evidence")):
        if r.get(key):
            lines += [f"## {title}", "", "```", _plain(r[key]), "```", ""]
    if r["stopped"]:
        lines += [f"Stopped early: {r['stopped']}", ""]
    if r["errors"]:
        lines += ["## Errors", "", *[f"- {e}" for e in r["errors"]], ""]
    if r["candidates"] and r["verified"] / r["candidates"] < 1 / 50:
        lines += ["**Fewer than 1 sendable email per 50 candidates: live, but not worth a campaign yet.**", ""]
    return "\n".join(lines)


def _plain(v) -> str:
    import json
    return json.dumps(v, indent=2, default=str)


def write_report(r: dict, out_dir: str = "docs/strategies") -> str:
    path = Path(out_dir) / f"{r['strategy']}-probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(r), encoding="utf-8")
    return str(path)
