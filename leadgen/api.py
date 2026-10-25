"""Engine API (Cloud Run service solomon-leadgen-api, PRIVATE).

Only The Creator's Outreach surface may invoke it (run.invoker on its runtime
service account); Cloud Run IAM enforces that before a request reaches this code.
The Creator never touches leadgen_* collections itself: every read and write
goes through here, so the engine owns its schema.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import Body, FastAPI, HTTPException

from . import campaigns as camp
from . import industries as inds
from . import strategies
from .llm import BudgetStop
from .prospect import Prospector
from .store import utcnow
from .strategist import PlanError, Strategist, apply_plan

log = logging.getLogger(__name__)

TARGET_LABEL = {"any": "any business with a website", "weak": "weak websites only",
                "weak_or_none": "weak websites or no website",
                "new_listing": "new businesses (no website, few reviews)"}


def create_app(engine_factory) -> FastAPI:
    app = FastAPI(title="solomon-leadgen-api", docs_url=None, redoc_url=None, openapi_url=None)
    state = {}

    def eng():
        if "e" not in state:
            state["e"] = engine_factory()
            camp.ensure_seed(state["e"].ledger, state["e"].settings)
        e = state["e"]
        e.report = {"errors": []}
        e.sync_inventory()   # pick up keys and mailboxes the latest health run found
        return e

    def get_c(e, cid):
        c = e.ledger.campaign(cid)
        if not c:
            raise HTTPException(404, "No such campaign. Refresh the list.")
        return c

    def bad(exc):
        raise HTTPException(400, str(exc))

    @app.get("/health")
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "service": "solomon-leadgen-api"}

    @app.get("/summary")
    def summary():
        return eng().summary()

    @app.get("/readiness")
    def readiness():
        return eng().readiness()

    @app.get("/capabilities")
    def capabilities():
        e = eng()
        rows = []
        for rot in (e.discover, e.scrape, e.reader, e.verify, e.people, e.sizing):
            if not rot:
                continue
            for entry in rot.entries:
                if entry["name"] == "direct":
                    continue
                rows.append({"stage": rot.stage, "name": entry["name"], "registry_name": entry.get("registry_name"),
                             "period": entry.get("period"), "cap": entry.get("cap"),
                             "free_amount": entry.get("free_amount"), "remaining": rot.remaining(entry),
                             "card_on_file": entry.get("card_on_file"), "secret": entry.get("secret"),
                             "kind": entry.get("kind")})
        scout = e.ledger.state("scout")
        return {"inventory": e.inv, "rotation": rows, "proposed": scout.get("proposals", []),
                "scout_month": scout.get("month")}

    @app.get("/campaigns")
    def list_campaigns():
        e = eng()
        camp.ensure_seed(e.ledger, e.settings)
        out = []
        for c in sorted(e.ledger.campaigns(), key=lambda c: c.get("created_at", ""), reverse=True):
            out.append({k: c.get(k) for k in ("id", "name", "status", "landing_url", "created_at", "updated_at",
                                              "paused_reason", "private")} | {"cells": len(c["icp"]["cells"]),
                                                                             "settings": c.get("settings")}
                       | _strategy_fields(c))
        return {"campaigns": out}

    @app.get("/industries")
    def industries():
        return inds.public_list()

    @app.get("/strategies")
    def list_strategies():
        """The live strategies: what the Strategy dropdown and the strategist offer."""
        return {"strategies": strategies.public_list(), "default": strategies.DEFAULT}

    @app.post("/campaigns")
    def create(body: dict = Body(...)):
        e = eng()
        try:
            sid = str(body.get("strategy") or strategies.DEFAULT)
            s = strategies.entry(sid)
            if s["status"] != "live":
                raise camp.CampaignError(f"{s['label']} is still being built")
            if s["uses_industry_list"]:
                ind = inds.get(str(body["industry"])) if body.get("industry") else None
                cells = inds.cells_for(ind) if ind else None
            else:
                # Segments come from the strategy's own data: the operator names the segment.
                ind = None
                segment = str(body.get("segment") or "").strip()
                if not segment:
                    raise camp.CampaignError(f"{s['label']} needs a segment name (what these leads are)")
                geos = [str(g) for g in body.get("geos") or []] or inds.catalog()["default_geos"]
                cells = [{"segment": segment, "geo": g, "query": segment} for g in geos]
            c = camp.new_campaign(e.settings, name=str(body.get("name", "")), description=str(body.get("description", "")),
                                  landing_url=str(body.get("landing_url", "")), booking_url=str(body.get("booking_url", "")),
                                  offer={"name": str(body.get("offer_name", "") or body.get("name", ""))},
                                  cells=cells, private=bool(body.get("private")))
            if ind:
                c["industry"] = ind["slug"]
            if body.get("strategy"):
                c["icp"] = strategies.normalize_icp({**c["icp"], "strategy": sid, "site_filter": strategies.TO_SITE_FILTER.get(sid, "any"),
                                                     "strategy_params": dict(body.get("strategy_params") or {}),
                                                     "strategy_source": "operator"})
            camp.validate(c)
        except (camp.CampaignError, inds.IndustryError, strategies.StrategyError, KeyError) as exc:
            bad(exc)
        e.ledger.save_campaign(c["id"], c)
        e.ledger.event("campaign_created", f"Campaign \"{c['name']}\" created as a draft.", campaign_id=c["id"])
        return strategies.view(e.ledger.campaign(c["id"]))

    @app.get("/campaigns/{cid}")
    def get_campaign(cid: str):
        return strategies.view(get_c(eng(), cid))

    @app.patch("/campaigns/{cid}")
    def patch(cid: str, body: dict = Body(...)):
        e = eng()
        c = get_c(e, cid)
        icp_body = body.get("icp") if isinstance(body.get("icp"), dict) else {}
        new_filter = icp_body.get("site_filter")
        before = strategies.strategy_id(c)
        asked = icp_body.get("strategy") or (strategies.FROM_SITE_FILTER.get(new_filter) if new_filter else None)
        params_change = "strategy_params" in icp_body and icp_body["strategy_params"] != \
            strategies.params(c, before) and (not asked or asked == before)
        if ((asked and asked != before) or params_change) and c["status"] not in ("draft", "paused"):
            # A campaign's audience doesn't change mid-run: the test batch and reply rates would
            # stop meaning anything. Pause first.
            raise HTTPException(409, "Who to target can only change while the campaign is a draft or paused. Pause it first.")
        if asked:
            body = {**body, "icp": {**icp_body, "strategy_source": "operator"}}
        try:
            nxt = camp.apply_patch(c, body)
        except (camp.CampaignError, strategies.StrategyError) as exc:
            bad(exc)
        after = strategies.strategy_id(nxt)
        if after != before:
            label = TARGET_LABEL.get(new_filter) if new_filter else None
            e.ledger.event("site_filter", f"\"{c['name']}\" now targets: {label or strategies.entry(after)['label']}.",
                           campaign_id=cid, why="Changed by the operator in the dashboard.")
        if "sequence" in body:
            nxt["sequence_edited"] = True
        e.ledger.save_campaign(cid, nxt)
        if "settings" in body and "writer_tier" in body["settings"]:
            e.ledger.event("writer_tier", f"\"{c['name']}\" writer set to {body['settings']['writer_tier']} by the operator.",
                           campaign_id=cid, notify=True, why="Manual choice in the dashboard.",
                           cost_impact="Sonnet costs about 20x Haiku per email." if body["settings"]["writer_tier"] == "sonnet" else "Lower AI cost.")
        return strategies.view(e.ledger.campaign(cid))

    @app.post("/campaigns/{cid}/cells")
    def add_cell(cid: str, body: dict = Body(...)):
        e = eng()
        c = get_c(e, cid)
        cell = {"segment": body.get("segment"), "geo": body.get("geo"), "query": body.get("query") or body.get("segment"),
                "naics": body.get("naics", "")}
        cells = c["icp"]["cells"] + [cell]
        try:
            nxt = camp.apply_patch(c, {"icp": {"cells": cells}})
        except camp.CampaignError as exc:
            bad(exc)
        e.ledger.save_campaign(cid, nxt)
        e.ledger.event("cell_added", f"Added {cell['segment']} in {cell['geo']} to \"{c['name']}\".", campaign_id=cid)
        return nxt

    @app.post("/campaigns/{cid}/clone")
    def clone(cid: str, body: dict = Body(...)):
        """A new DRAFT for another industry: same offer, sequence, settings, towns and target;
        the industry's own search term (and, for Website Launch, its pages, headline and FAQ)."""
        e = eng()
        src = get_c(e, cid)
        if not strategies.entry(strategies.strategy_id(src))["uses_industry_list"]:
            bad(camp.CampaignError("This campaign's segments come from its strategy's own data, not the "
                                   "industries list, so it can't be cloned for an industry."))
        try:
            ind = inds.get(str(body.get("industry", "")))
        except inds.IndustryError as exc:
            bad(exc)
        if (src.get("industry") or (src.get("preview") or {}).get("industry")) == ind["slug"]:
            bad(camp.CampaignError("That's this campaign's industry already. Pick a different one."))
        name = inds.clone_name(src, ind)
        if any(x["name"] == name for x in e.ledger.campaigns()):
            raise HTTPException(409, f"\"{name}\" already exists.")
        geos = list(dict.fromkeys(x["geo"] for x in src["icp"]["cells"])) or None
        try:
            c = camp.new_campaign(e.settings, name=name, description=src.get("description", ""),
                                  landing_url=src["landing_url"], booking_url=src.get("booking_url", ""),
                                  offer=dict(src["offer"]), cells=inds.cells_for(ind, geos))
            c["description"] = c["description"].replace(
                (src["icp"]["cells"][0]["segment"].lower() if src["icp"]["cells"] else "\0"), ind["label"].lower())
            c["industry"] = ind["slug"]
            c["icp"] = {**src["icp"], "cells": c["icp"]["cells"]}
            c["sequence"] = [dict(s) for s in src["sequence"]]
            c["sequence_edited"] = bool(src.get("sequence_edited"))
            c["settings"] = {**src["settings"]}
            c["preview"] = dict(src.get("preview") or c["preview"])
            if camp.offers_preview(src):
                c.update(inds.website_launch_fields(ind, src))
            c["cloned_from"] = src["id"]
            camp.validate(c)
        except (camp.CampaignError, inds.IndustryError) as exc:
            bad(exc)
        e.ledger.save_campaign(c["id"], c)
        e.ledger.event("campaign_created", f"Campaign \"{c['name']}\" created as a draft (cloned from \"{src['name']}\").",
                       campaign_id=c["id"])
        return strategies.view(e.ledger.campaign(c["id"]))

    @app.post("/campaigns/{cid}/preflight")
    def preflight_now(cid: str):
        """Run preflight now instead of on the next tick. Starts the job and returns at once."""
        e = eng()
        c = get_c(e, cid)
        if c["status"] != "waiting":
            bad(camp.CampaignError(f"Preflight runs for a launched campaign that is waiting; this one is {c['status']}."))
        if e.preflight_in_flight(c):
            raise HTTPException(409, "A preflight is already running for this campaign.")
        try:
            e.start_preflight_job(cid)
        except Exception as exc:  # noqa: BLE001 - surface the reason; the tick still runs it later
            log.warning("could not start preflight job for %s: %s", cid, exc)
            raise HTTPException(502, f"Could not start preflight ({type(exc).__name__}). The next tick still runs it.")
        return e.ledger.campaign(cid)

    @app.post("/campaigns/{cid}/plan")
    def plan(cid: str):
        e = eng()
        c = get_c(e, cid)
        s = Strategist(e.settings, e.ledger, e.llm, e.registry, e.inv, discover=e.discover, scrape=e.reader,
                       sizing=e.sizing, models=e.models)
        try:
            p = s.plan(c, mailboxes=len(e.mailbox_pool()))
        except PlanError as exc:
            raise HTTPException(422, f"The strategist's plan did not validate: {exc}")
        except BudgetStop as exc:
            raise HTTPException(429, str(exc))
        nxt = apply_plan(c, p)
        e.ledger.save_campaign(cid, nxt)
        e.ledger.event("plan", f"Strategist plan ready for \"{c['name']}\".", campaign_id=cid,
                       cost_impact=f"Estimated ${p['est_monthly_ai_cost_usd']:.2f}/month AI at full volume.")
        return p

    @app.post("/campaigns/{cid}/samples")
    def samples(cid: str, body: dict | None = Body(None)):
        e = eng()
        c = get_c(e, cid)
        n = int((body or {}).get("n", 3))
        since = utcnow()
        try:
            drafts = Prospector(e).samples(c, n=max(1, min(n, 5)))
        except BudgetStop as exc:
            raise HTTPException(429, str(exc))
        costs = [r for r in e.ledger.costs_since(since) if r.get("campaign_id") == cid]
        return {"drafts": drafts, "cost_usd": round(sum(r["usd"] for r in costs), 4),
                "cost_records": [r["id"] for r in costs], "errors": e.report["errors"][:10]}

    @app.post("/campaigns/{cid}/launch")
    def launch(cid: str):
        e = eng()
        get_c(e, cid)
        try:
            # Returns at once; the next tick (every 10 min, 8am-7pm ET) runs preflight.
            return e.launch(cid, run_preflight=False)
        except camp.CampaignError as exc:
            bad(exc)

    @app.post("/campaigns/{cid}/pause")
    def pause(cid: str):
        e = eng()
        c = get_c(e, cid)
        e.ledger.save_campaign(cid, {"status": "paused", "paused_reason": "paused by the operator",
                                     "paused_at": utcnow().isoformat(), "status_before_pause": c["status"]})
        e.ledger.event("campaign_paused", f"\"{c['name']}\" paused.", why="Paused by the operator.", campaign_id=cid)
        return e.ledger.campaign(cid)

    @app.post("/campaigns/{cid}/resume")
    def resume(cid: str):
        e = eng()
        c = get_c(e, cid)
        if c["status"] != "paused":
            bad(f"campaign is {c['status']}, not paused")
        tested = (c.get("test") or {}).get("passed")
        before = c.get("status_before_pause")
        nxt = "active" if tested or before == "active" else ("testing" if before == "testing" else "waiting")
        e.ledger.save_campaign(cid, {"status": nxt, "paused_reason": None})
        e.ledger.event("campaign_resumed", f"\"{c['name']}\" resumed ({nxt}).", why="Resumed by the operator.",
                       campaign_id=cid, notify=True, cost_impact="Sending restarts inside the daily cap.")
        return e.ledger.campaign(cid)

    @app.post("/campaigns/{cid}/done")
    def done(cid: str):
        e = eng()
        c = get_c(e, cid)
        e.ledger.save_campaign(cid, {"status": "done", "done_at": utcnow().isoformat()})
        e.ledger.event("campaign_done", f"\"{c['name']}\" marked done.", campaign_id=cid)
        return e.ledger.campaign(cid)

    @app.get("/campaigns/{cid}/detail")
    def detail(cid: str):
        e = eng()
        c = get_c(e, cid)
        leads = e.ledger.leads(cid)
        funnel = []
        for s in c["sequence"]:
            funnel.append({"step": s["step"], "day": s["day"],
                           "sent": sum(1 for l in leads if s["step"] in (l.get("seq") or {}).get("sent_steps", []))})
        counts = {}
        # Why leads didn't go forward, and what the site check saw (no lead-level data leaves here).
        why = {"exclusion_reasons": {}, "site_checks": {}, "no_website_leads": 0, "text_only_reads": 0,
               "weak_site_leads": 0}
        for l in leads:
            counts[l.get("status", "?")] = counts.get(l.get("status", "?"), 0) + 1
            if l.get("reason"):
                why["exclusion_reasons"][l["reason"]] = why["exclusion_reasons"].get(l["reason"], 0) + 1
            for r in ((l.get("site_quality") or {}).get("reasons") or ["none_found"]) if l.get("site_quality") else []:
                why["site_checks"][r] = why["site_checks"].get(r, 0) + 1
            why["no_website_leads"] += 1 if l.get("nosite") else 0
            why["weak_site_leads"] += 1 if (l.get("site_quality") or {}).get("weak") else 0
            why["text_only_reads"] += 1 if l.get("status") == "site_unassessed" else 0
        threads = [t for _, t in e.ledger.s.where("threads", ("campaign_id", "==", cid))]
        replies = []
        for t in sorted(threads, key=lambda t: t.get("updated_at", ""), reverse=True)[:50]:
            ins = [m for m in t.get("messages", []) if m.get("dir") == "in"]
            if not ins:
                continue
            last = ins[-1]
            replies.append({"thread_id": t["id"], "domain": t.get("domain"), "class": t.get("last_class"),
                            "state": t.get("state"), "ai_replies": t.get("ai_replies", 0), "at": last.get("at"),
                            "snippet": (last.get("body") or "")[:240],
                            "gmail_url": f"https://mail.google.com/mail/#search/rfc822msgid%3A{(last.get('message_id') or '').strip('<>')}"})
        upcoming = []
        for l in leads:
            seq = l.get("seq") or {}
            st = seq.get("next_step")
            if l.get("status") == "in_sequence" and st is not None and str(st) in (seq.get("drafts") or {}):
                d = seq["drafts"][str(st)]
                upcoming.append({"domain": l["domain"], "name": l.get("name"), "step": st, "due": seq.get("next_due"),
                                 "subject": d.get("subject"), "body": d.get("body"), "source": d.get("source")})
        upcoming.sort(key=lambda x: (x["due"] or "", x["step"]))
        return {"campaign": strategies.view(c), "funnel": funnel, "lead_counts": counts, "lead_outcomes": why,
                "cells": [{"id": x["id"], "segment": x["segment"], "geo": x["geo"],
                           **((c.get("cells_state") or {}).get(x["id"]) or {"share": None, "sends": 0, "positive": 0})}
                          for x in c["icp"]["cells"]],
                "replies": replies, "upcoming": upcoming[:30], "events": e.ledger.events(campaign_id=cid, limit=50)}

    @app.get("/events")
    def events(days: int = 14):
        return {"events": eng().ledger.events(since=utcnow() - timedelta(days=max(1, min(days, 90))), limit=200)}

    @app.get("/settings")
    def get_settings():
        e = eng()
        return {"ai_monthly_cap_usd": e.budget.cap,
                "paid_providers_monthly_cap_usd": e.settings["budget"]["paid_providers_monthly_cap_usd"],
                "reply_reserve_share": e.settings["budget"]["reply_reserve_share"],
                "kill_switch": e.ledger.kill_switch_on(), "spend": e.budget.spend(),
                "mailboxes": e.readiness()["mailboxes"]}

    @app.put("/settings")
    def put_settings(body: dict = Body(...)):
        e = eng()
        if "paid_providers_monthly_cap_usd" in body and float(body["paid_providers_monthly_cap_usd"]) != 0:
            bad("Paid provider spend is fixed at $0 (operator decision 2026-10-22): free allowances only.")
        if "ai_monthly_cap_usd" in body:
            cap = float(body["ai_monthly_cap_usd"])
            if not 0 <= cap <= 500:
                bad("ai_monthly_cap_usd must be between 0 and 500")
            e.ledger.set_state("settings", ai_monthly_cap_usd=cap)
            e.ledger.event("settings", f"AI monthly cap set to ${cap:.0f}.", why="Changed in the dashboard.",
                           cost_impact=f"New ceiling ${cap:.0f}/month.")
        return get_settings()

    @app.post("/kill-switch")
    def kill(body: dict = Body(...)):
        e = eng()
        on = bool(body.get("on"))
        e.ledger.set_state("kill_switch", on=on, reason=body.get("reason") or "set by operator", at=utcnow().isoformat())
        e.ledger.event("kill_switch", f"Global kill switch turned {'on' if on else 'off'}.",
                       why="Set by the operator.", notify=True,
                       cost_impact="All sending stops before the next message." if on else "Sending may resume.")
        return {"kill_switch": on}

    return app


def _strategy_fields(c: dict) -> dict:
    v = strategies.view(c)
    return {"strategy": v["icp"]["strategy"], "strategy_label": v["strategy_label"],
            "strategy_params": v["icp"]["strategy_params"]}


def app_from_env() -> FastAPI:
    import os
    from .engine import make_engine
    local = os.environ.get("LEADGEN_LOCAL") == "1"   # dev: .state/leadgen.json + env-var keys
    return create_app(lambda: make_engine(local=local))
