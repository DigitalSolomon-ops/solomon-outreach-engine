"""The engine: wiring plus the three scheduled jobs and the launch state machine.

  health   05:47 ET  key inventory, mailbox DNS/blacklist, landing checks, test-batch evaluations,
                     monthly provider scout (first run of the month)
  prospect 06:17 ET  per sending campaign: rebalance cells -> discover -> enrich -> brief -> first
                     touches for the day; due follow-ups; escalation and budget checks; digest
  tick     every 10 min 08:00-19:59 ET  inbox -> classify -> reply/escalate; preflight for
                     waiting campaigns; test evaluation; kill rules; send what is due
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta

import httpx

from . import campaigns as camp
from .allocate import rebalance
from .capabilities import inventory, load_registry, rotation_entries
from .config import load_models, load_settings
from .llm import LLM, Budget, BudgetStop, month_start
from .mailboxes import domain_health, load_pool, mailbox_retire_check, bounce_rate
from .notify import Notifier
from .prospect import Prospector
from .providers import STAGES
from .replies import ReplyAgent
from .rotation import Rotation
from .sender import TickSender, today_et
from .store import utcnow
from .writer import Writer, escalation_reason

log = logging.getLogger(__name__)
NEGATIVE = {"negative", "legal", "optout"}


class Engine:
    def __init__(self, settings: dict, ledger, secrets, *, registry=None, models=None, llm_client=None,
                 smtp_factory=None, imap_factory=None, secret_names=None, dns_resolver=None, http=None):
        self.settings = settings
        self.ledger = ledger
        self.secrets = secrets
        self.registry = registry or load_registry()
        self.models = models or load_models()
        self._secret_names = secret_names       # callable -> set[str] (tests), else Secret Manager listing
        self.smtp_factory = smtp_factory
        self.imap_factory = imap_factory
        self.dns_resolver = dns_resolver
        self.http = http or httpx
        self.report = {"errors": []}
        self.notifier = Notifier(settings, ledger, secrets, sender_creds=self._first_mailbox_creds,
                                 **({"smtp_factory": smtp_factory} if smtp_factory else {}))
        self.budget = Budget(settings, ledger, self.notifier)
        self.llm = LLM(secrets.get(settings["anthropic_secret"]), self.models, ledger, self.budget,
                       client=llm_client)
        self.writer = Writer(self.llm, settings, ledger)
        self.inv = self._cached_inventory()
        self._build_rotations()

    # ------------------------------------------------------------------ wiring
    def secret_names(self) -> set[str]:
        if self._secret_names:
            return set(self._secret_names())
        return set(self.secrets.list_names(self._all_known_names()))

    def _all_known_names(self) -> list[str]:
        names = set()
        for p in self.registry["providers"]:
            names.update(n for n in [p.get("secret"), *p.get("secret_aliases", [])] if n)
        names.update([self.settings["anthropic_secret"], self.settings["unsubscribe"]["hmac_secret"],
                      self.settings["notify"]["smtp_secret"], self.settings["ghl"]["secret"],
                      ])
        names.update(f"{self.settings['send']['mailbox_secret_prefix']}{i}" for i in range(1, 11))
        return sorted(names)

    def refresh_inventory(self) -> dict:
        self.inv = inventory(self.registry, self.secret_names())
        self.ledger.set_state("capabilities", **self.inv)
        self._build_rotations()
        return self.inv

    def sync_inventory(self) -> bool:
        """Adopt the inventory the latest health run wrote, if it is newer than ours.
        Long-lived processes (the API) call this per request; keys added in Credentials
        then show up without a restart. Returns True when something changed."""
        latest = self.ledger.state("capabilities")
        if not latest.get("providers") or latest.get("at") == self.inv.get("at"):
            return False
        self.inv = latest
        clear = getattr(self.secrets.get, "cache_clear", None)
        if clear:
            clear()                    # forget "secret missing" answers from before the key existed
        self._build_rotations()
        return True

    def _cached_inventory(self) -> dict:
        cached = self.ledger.state("capabilities")
        if cached.get("providers"):
            return cached
        return inventory(self.registry, self.secret_names())

    def _build_rotations(self):
        ov = self.settings["rotation"]["overrides"]
        margin = self.settings["rotation"]["free_cap_margin"]
        cb = self.notifier.provider_exhausted

        def rot(stage, extra=(), exclude=()):
            entries = [e for e in rotation_entries(self.registry, self.inv, stage, STAGES[stage], ov, margin)
                       if e["name"] not in exclude] + list(extra)
            return Rotation(stage, entries, STAGES[stage], self.secrets, self.ledger,
                            on_exhausted=lambda n, r, b: cb(n, r, b))

        self.discover = rot("discovery")
        # Raw-HTML readers for the observation pass; direct fetch is the free last resort.
        self.scrape = rot("scrape", extra=[{"name": "direct"}], exclude=("jina_reader",))
        self.reader = rot("scrape", exclude=tuple(n for n in STAGES["scrape"] if n != "jina_reader"))
        self.verify = rot("verify", exclude=("mx_check",))
        self.people = rot("people") if STAGES["people"] else None
        self.sizing = rot("sizing") if STAGES["sizing"] else None

    def unsub_base(self) -> str:
        return (self.settings["unsubscribe"].get("base_url") or os.environ.get("LEADGEN_UNSUB_URL", "")).rstrip("/")

    def hmac_key(self) -> str | None:
        return self.secrets.get(self.settings["unsubscribe"]["hmac_secret"])

    def mailbox_pool(self, healthy_only: bool = True) -> list[dict]:
        pool = load_pool(self.secrets, self.inv.get("mailbox_secrets", []))
        out = []
        for mb in pool:
            if "creds" not in mb:
                continue
            doc = self.ledger.s.get("mailboxes", mb["id"]) or {}
            mb["health"] = doc.get("health")
            mb["status"] = doc.get("status", "active")
            if healthy_only and (mb["status"] == "retired" or not (mb["health"] or {}).get("ok")):
                continue
            out.append(mb)
        return out

    def _first_mailbox_creds(self):
        pool = self.mailbox_pool()
        return pool[0]["creds"] if pool else None

    def _run_doc(self, kind: str, started: datetime, body: dict) -> str:
        rid = f"{kind}-{started.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.ledger.save_run(rid, {"id": rid, "kind": kind, "started_at": started.isoformat(),
                                   "finished_at": utcnow().isoformat(), **body,
                                   "errors": self.report["errors"][:30]})
        return rid

    # ------------------------------------------------------------------ health
    def health(self, now: datetime | None = None) -> dict:
        started = utcnow()
        camp.ensure_seed(self.ledger, self.settings)
        inv = self.refresh_inventory()
        mbs = []
        for mb in self.mailbox_pool(healthy_only=False):
            h = domain_health(mb["domain"], mb["creds"].get("dkim_selector"), resolver=self.dns_resolver)
            doc = self.ledger.s.get("mailboxes", mb["id"]) or {}
            self.ledger.s.set("mailboxes", mb["id"], {"id": mb["id"], "email": mb["email"], "domain": mb["domain"],
                                                     "health": h, "status": doc.get("status", "active")})
            mbs.append({"id": mb["id"], "domain": mb["domain"], **{k: h[k] for k in ("spf", "dkim", "dmarc", "dbl", "ok")}})
            reason = mailbox_retire_check(self.settings["send"], self.ledger, mb["id"], h)
            if reason and doc.get("status") != "retired":
                self._retire(mb, reason)
        landing = {}
        # Drafts too: the Readiness panel shows whether the landing page is live before Launch.
        for c in self.ledger.campaigns("draft", "waiting", "testing", "active", "paused"):
            landing[c["id"]] = self.check_landing(c)
        evals = self.evaluate_tests()
        scout = self.maybe_scout(now)
        self.notifier.flush()
        rid = self._run_doc("health", started, {"inventory": {"connected": inv["connected"],
                                                              "matched_secret_names": inv["matched_secret_names"]},
                                                "mailboxes": mbs, "landing": landing, "test_evaluations": evals,
                                                "scout": scout})
        return {"run_id": rid, "connected": inv["connected"], "mailboxes": mbs, "landing": landing,
                "test_evaluations": evals, "scout": scout}

    def check_landing(self, c: dict) -> dict:
        """ok = the page answers 2xx (Readiness). preflight_ok also needs it to mention the offer
        (the launch preflight's stricter rule)."""
        url = c["landing_url"]
        try:
            r = self.http.get(url, timeout=10, follow_redirects=True,
                              headers={"User-Agent": "SolomonLeadgen/1.0 (+https://digitalsolomon.com)"})
            text = r.text.lower()
        except Exception as exc:  # noqa: BLE001
            out = {"url": url, "ok": False, "preflight_ok": False, "status": None,
                   "why": type(exc).__name__, "checked_at": utcnow().isoformat()}
            self.ledger.save_campaign(c["id"], {"landing_check": out})
            return out
        words = [w for w in camp.slug(c["offer"]["name"]).split("-") if len(w) > 2]
        mentions = bool(words) and sum(w in text for w in words) >= max(1, len(words) - 1)
        reachable = 200 <= r.status_code < 300
        out = {"url": url, "ok": reachable, "preflight_ok": reachable and mentions, "status": r.status_code,
               "mentions_offer": mentions, "checked_at": utcnow().isoformat()}
        self.ledger.save_campaign(c["id"], {"landing_check": out})
        return out

    def unsub_healthy(self) -> bool:
        base = self.unsub_base()
        if not base.startswith("https://"):
            return False
        try:
            r = self.http.get(base + "/health", timeout=15)
            return r.status_code == 200 and r.json().get("ok") is True
        except Exception:  # noqa: BLE001
            return False

    def _retire(self, mb: dict, reason: str):
        self.ledger.s.set("mailboxes", mb["id"], {"status": "retired", "retired_reason": reason,
                                                 "retired_at": utcnow().isoformat()})
        self.ledger.event("mailbox_retired", f"Mailbox {mb['email']} retired.", why=reason,
                          cost_impact="Sending capacity drops by up to 30/day until a new domain is added. "
                                      "Recommended: buy a new sending domain and add its mailbox to Credentials.",
                          notify=True)

    # ------------------------------------------------------------------ preflight + launch
    def preflight(self, c: dict, samples: int = 5) -> dict:
        checks = {}
        land = self.check_landing(c)
        checks["landing"] = {"ok": land.get("preflight_ok", False),
                             "detail": f"HTTP {land.get('status')}, mentions offer: {land.get('mentions_offer')}"}
        checks["unsubscribe"] = {"ok": self.unsub_healthy(), "detail": self.unsub_base() or "service URL not set"}
        pool = self.mailbox_pool()
        checks["mailbox"] = {"ok": bool(pool), "detail": f"{len(pool)} healthy mailbox(es)"}
        ok_b, why_b = self.budget.allowed("first_touch")
        checks["budget"] = {"ok": ok_b, "detail": why_b or "headroom available"}
        checks["kill_switch"] = {"ok": not self.ledger.kill_switch_on(), "detail": "off" if not self.ledger.kill_switch_on() else "on"}
        if c.get("private") and not c["icp"]["cells"]:
            # A private test campaign (the loopback) has no prospect cells to sample from.
            checks["samples"] = {"ok": all(v["ok"] for v in checks.values()),
                                 "detail": "skipped: private test campaign with no prospect cells"}
        elif all(v["ok"] for v in checks.values()):
            try:
                drafts = Prospector(self).samples(c, n=samples)
                good = [d for d in drafts if d["passed"]]
                checks["samples"] = {"ok": len(good) >= samples, "detail": f"{len(good)}/{samples} drafts passed validator and critic"}
            except BudgetStop as exc:
                checks["samples"] = {"ok": False, "detail": str(exc)}
        else:
            checks["samples"] = {"ok": False, "detail": "waits for the checks above"}
        result = {"at": utcnow().isoformat(), "ok": all(v["ok"] for v in checks.values()), "checks": checks}
        self.ledger.save_campaign(c["id"], {"preflight": result})
        return result

    def launch(self, cid: str) -> dict:
        c = self.ledger.campaign(cid)
        if not c:
            raise camp.CampaignError("no such campaign")
        if c["status"] not in ("draft", "paused"):
            raise camp.CampaignError(f"campaign is {c['status']}; only a draft or paused campaign can be launched")
        camp.validate(c)
        self.ledger.save_campaign(cid, {"status": "waiting", "launched_at": utcnow().isoformat()})
        self.ledger.event("campaign_launched", f"Campaign \"{c['name']}\" launched; waiting for preflight.",
                          why="Launched by the operator.", campaign_id=cid)
        return self.advance_waiting(self.ledger.campaign(cid))

    def advance_waiting(self, c: dict) -> dict:
        pf = self.preflight(c)
        if pf["ok"]:
            self.ledger.save_campaign(c["id"], {"status": "testing",
                                                "test": {"started_at": utcnow().isoformat(), "evaluated": False}})
            self.ledger.event("campaign_testing", f"\"{c['name']}\" passed preflight and is sending its test batch.",
                              why="All preflight checks are green.", campaign_id=c["id"], notify=True,
                              cost_impact=f"Test batch of {c['settings']['test_batch_size']} first touches.")
        return {"status": (self.ledger.campaign(c["id"]) or {}).get("status"), "preflight": pf}

    def evaluate_tests(self) -> list[dict]:
        out = []
        cfg = self.settings["send"]
        for c in self.ledger.campaigns("testing"):
            sends = [s for _, s in self.ledger.s.where("sends", ("campaign_id", "==", c["id"]))
                     if s.get("step") == 0 and s.get("status") == "sent"]
            batch = int(c["settings"].get("test_batch_size", cfg["test_batch_size"]))
            if not sends:
                continue
            last = max(s["sent_at"] for s in sends)
            first = min(s["sent_at"] for s in sends)
            done = len(sends) >= batch or datetime.fromisoformat(first) < utcnow() - timedelta(days=7)
            if not done or datetime.fromisoformat(last) > utcnow() - timedelta(hours=cfg["test_evaluate_hours"]):
                continue
            bounces = sum(1 for s in sends if s.get("bounced"))
            bad = [l for l in self.ledger.leads(c["id"]) if l.get("reply_class") in ("legal", "negative")]
            passed = bounces <= cfg["test_max_hard_bounces"] and not bad
            why = (f"{bounces} hard bounce(s) and {len(bad)} angry or legal reply(ies) in {len(sends)} test sends")
            self.ledger.save_campaign(c["id"], {"status": "active" if passed else "paused",
                                                "test": {**(c.get("test") or {}), "evaluated": True, "passed": passed,
                                                         "evaluated_at": utcnow().isoformat(), "result": why}})
            self.ledger.event("campaign_active" if passed else "campaign_paused",
                              f"\"{c['name']}\" {'passed its test and is now active' if passed else 'failed its test and is paused'}.",
                              why=why, campaign_id=c["id"], notify=True,
                              cost_impact="Ramps to the full daily cap." if passed else "Sending for this campaign stops.")
            out.append({"campaign_id": c["id"], "passed": passed, "why": why})
        return out

    def kill_rules(self) -> list[str]:
        cfg = self.settings["send"]
        out = []
        for c in self.ledger.campaigns("testing", "active"):
            sends7 = self.ledger.sends_since(utcnow() - timedelta(days=7), campaign_id=c["id"])
            rate, b, n = bounce_rate(sends7)
            why = None
            if n >= cfg["campaign_pause_bounce_min"] and rate > cfg["campaign_pause_bounce_rate"]:
                why = f"7-day hard-bounce rate {rate:.1%} ({b}/{n}) is over {cfg['campaign_pause_bounce_rate']:.0%}"
            leads = [l for l in self.ledger.leads(c["id"]) if (l.get("seq") or {}).get("sent_steps")]
            neg = sum(1 for l in leads if l.get("reply_class") in NEGATIVE or l.get("status") == "suppressed")
            if not why and len(leads) >= cfg["campaign_pause_negative_min"] and neg / len(leads) > cfg["campaign_pause_negative_rate"]:
                why = f"unsubscribes plus negative replies are {neg / len(leads):.1%} ({neg}/{len(leads)})"
            if why:
                self.ledger.save_campaign(c["id"], {"status": "paused", "paused_reason": why,
                                                    "paused_at": utcnow().isoformat()})
                self.ledger.event("campaign_paused", f"\"{c['name']}\" paused automatically.", why=why,
                                  campaign_id=c["id"], notify=True, cost_impact="Sending for this campaign stops.")
                out.append(c["id"])
        for mb in self.mailbox_pool():
            reason = mailbox_retire_check(cfg, self.ledger, mb["id"], mb.get("health"))
            if reason:
                self._retire(mb, reason)
        return out

    # ------------------------------------------------------------------ tick
    def tick(self, now: datetime | None = None) -> dict:
        started = utcnow()
        holder = uuid.uuid4().hex
        if not self.ledger.s.lease("tick", holder, timedelta(minutes=15)):
            return {"status": "skipped: another tick holds the lease"}
        try:
            expired = self.ledger.expire_stale_sending()
            pool = self.mailbox_pool()
            ghl = self.secrets.get_json(self.settings["ghl"]["secret"]) if self.settings["ghl"].get("enabled") else None
            replies = ReplyAgent(self.settings, self.ledger, self.llm, pool=pool, hmac_key=self.hmac_key(),
                                 unsub_base=self.unsub_base(), notifier=self.notifier, ghl_creds=ghl,
                                 imap_factory=self.imap_factory, smtp_factory=self.smtp_factory).poll() if pool else {}
            waiting = []
            for c in self.ledger.campaigns("waiting"):
                last = (c.get("preflight") or {}).get("at")
                if not last or datetime.fromisoformat(last) < utcnow() - timedelta(hours=1):
                    waiting.append({c["id"]: self.advance_waiting(c)["status"]})
            evals = self.evaluate_tests()
            paused = self.kill_rules()
            sender = TickSender(self.settings, self.ledger, pool=self.mailbox_pool(), hmac_key=self.hmac_key(),
                                unsub_base=self.unsub_base(),
                                **({"smtp_factory": self.smtp_factory} if self.smtp_factory else {}),
                                **({"now": (lambda: now)} if now else {}))
            sent = sender.run()
            self.budget.check_notices()
            self.notifier.flush()
            body = {"expired_sending": expired, "replies": replies, "waiting": waiting,
                    "test_evaluations": evals, "auto_paused": paused, "send": sent}
            rid = self._run_doc("tick", started, body)
            return {"run_id": rid, **body}
        finally:
            self.ledger.s.lease("tick", holder, timedelta(0), release=True)

    # ------------------------------------------------------------------ prospect
    def prospect(self) -> dict:
        started = utcnow()
        camp.ensure_seed(self.ledger, self.settings)
        out = {}
        pr = Prospector(self)
        for c in self.ledger.campaigns("testing", "active"):
            res = {}
            reason = escalation_reason(self.settings, self.ledger, c, *self._first_touch_stats(c))
            if reason and not c["settings"].get("writer_tier_manual"):
                self._escalate_writer(c, reason)
                c = self.ledger.campaign(c["id"])
            try:
                res["followups"] = pr.write_followups(c)
            except BudgetStop as exc:
                res["followups"] = {"stopped": str(exc)}
            target = self._first_touch_target(c)
            n_q = self.settings["discovery"]["queries_per_day"]
            queries, notices = rebalance(self.settings, self.ledger, c, n_q)
            for n in notices:
                self.ledger.event("icp_shift", f"\"{c['name']}\": {n}.", why="Thompson sampling on positive replies.",
                                  campaign_id=c["id"], notify=True, cost_impact="none")
            ok, why = self.budget.allowed("first_touch")
            if not ok:
                res["first_touches"] = {"stopped": why}
            elif target > 0:
                res["first_touches"] = pr.run_campaign(c, queries, target)
            else:
                res["first_touches"] = {"stopped": "enough first touches are queued"}
            out[c["id"]] = res
        self.budget.check_notices()
        digest = self.digest()
        self.notifier.flush()
        rid = self._run_doc("prospect", started, {"campaigns": out, "providers": self._provider_report()})
        return {"run_id": rid, "campaigns": out, "digest_sent": digest}

    def _first_touch_stats(self, c) -> tuple[int, int]:
        leads = self.ledger.leads(c["id"])
        sent = sum(1 for l in leads if 0 in (l.get("seq") or {}).get("sent_steps", []))
        pos = sum(1 for l in leads if l.get("reply_class") in ("interested", "question", "referral"))
        return sent, pos

    def _first_touch_target(self, c) -> int:
        queued = len([l for l in self.ledger.leads(c["id"], "in_sequence") if (l.get("seq") or {}).get("next_step") == 0])
        cap = int(c["settings"].get("daily_cap", 30))
        if c["status"] == "testing":
            sent = self._first_touch_stats(c)[0]
            cap = max(0, int(c["settings"].get("test_batch_size", 10)) - sent)
        return max(0, min(cap, self.settings["discovery"]["new_leads_per_day"]) - queued)

    def _escalate_writer(self, c, reason):
        ft = max(1, c["settings"].get("daily_cap", 30)) * 21 * len(c["sequence"])
        m = self.models
        diff = ft * (1900 / 1e6 * (m["sonnet"]["input_per_mtok"] - m["haiku"]["input_per_mtok"])
                     + 350 / 1e6 * (m["sonnet"]["output_per_mtok"] - m["haiku"]["output_per_mtok"]))
        self.ledger.save_campaign(c["id"], {"settings": {**c["settings"], "writer_tier": "sonnet"}})
        self.ledger.event("writer_escalated", f"\"{c['name']}\" now writes with Sonnet instead of Haiku.",
                          why=reason, campaign_id=c["id"], notify=True,
                          cost_impact=f"About +${diff:.2f}/month at full volume.")

    def _provider_report(self) -> dict:
        return {r.stage + ("_reader" if r is self.reader else ""): {"used": dict(r.used), "skipped": r.skipped,
                                                                    "errors": r.errors[:10]}
                for r in (self.discover, self.scrape, self.reader, self.verify, self.people, self.sizing) if r}

    # ------------------------------------------------------------------ digest + summary
    def digest(self) -> bool:
        y0 = utcnow() - timedelta(days=1)
        sends = self.ledger.sends_since(y0)
        events = self.ledger.events(since=y0)
        spend = self.budget.spend()
        replies = [e for e in events if e["kind"] in ("interested_reply", "reply_escalated")]
        due = sum(1 for c in self.ledger.campaigns("testing", "active") for l in self.ledger.leads(c["id"], "in_sequence")
                  if str((l.get("seq") or {}).get("next_due", "9")) <= (today_et() + timedelta(days=1)).isoformat())
        proposed = []
        for c in self.ledger.campaigns("testing", "active", "draft", "waiting"):
            have = {x["id"] for x in c["icp"]["cells"]}
            for x in ((c.get("plan") or {}).get("icp_cells") or []):
                if camp.cell_id(x["segment"], x["geo"]) not in have:
                    proposed.append(f"{c['name']}: {x['segment']} in {x['geo']}")
        recs = [f"{r['name']} -> paste a key in Credentials as {r['vault_slot']} ({r['why']})"
                for r in self.inv.get("recommended", [])[:8]]
        body = "\n".join([
            f"Sends in the last 24h: {len(sends)}",
            f"Replies needing you or interested: {len(replies)}",
            f"AI spend this month: ${spend['total']:.2f} of ${self.budget.cap:.0f}",
            f"Messages due through tomorrow: {due}", "",
            "Proposed new cells (Add them on the Outreach page):", *(proposed[:15] or ["none"]), "",
            "Recommended connections ($0):", *(recs or ["none"]), "",
            "Events:", *[f"- {e['what']}" for e in events[:30]]])
        return self.notifier.email(f"Outreach digest {today_et().isoformat()}: {len(sends)} sent, {len(replies)} replies", body)

    def summary(self) -> dict:
        spend = self.budget.spend()
        active = self.ledger.campaigns("testing", "active")
        from .mailboxes import et_day_start
        today = self.ledger.sends_since(et_day_start())
        need = [e for e in self.ledger.events(since=utcnow() - timedelta(days=7)) if e["kind"] == "reply_escalated"]
        spent_today = sum(r.get("usd", 0) for r in self.ledger.costs_since(et_day_start()))
        return {"activeCampaigns": len(active), "sentToday": len(today), "repliesNeedingMarcus": len(need),
                "aiSpendMonthUsd": round(spend["total"], 2), "aiCapUsd": self.budget.cap,
                "aiSpendTodayUsd": round(spent_today, 2), "killSwitch": self.ledger.kill_switch_on()}

    def readiness(self) -> dict:
        """Built from what the health job last WROTE (leadgen_state/capabilities and
        leadgen_mailboxes), never from this process's memory: an API instance can outlive
        several health runs. Secret values are not read here."""
        inv = self.ledger.state("capabilities") or self.inv
        names = set(inv.get("mailbox_secrets", []))
        docs = [d for _, d in self.ledger.s.all("mailboxes")]
        pool_all = [d for d in docs if f"{self.settings['send']['mailbox_secret_prefix']}{d.get('id')}" in names]
        healthy = [m for m in pool_all if (m.get("health") or {}).get("ok") and m.get("status") != "retired"]
        checked = max(((m.get("health") or {}).get("checked_at") or "" for m in pool_all), default="")
        seed = self.ledger.campaign(camp.SEED_ID) or {}
        land = seed.get("landing_check") or {}
        if land.get("checked_at"):
            land_detail = (f"HTTP {land.get('status')} at {land['checked_at'][:16].replace('T', ' ')} UTC"
                           + ("" if land.get("ok") else f" ({land.get('why') or 'not a 2xx'})")
                           + (" - page does not mention the offer yet; Launch preflight needs that"
                              if land.get("ok") and not land.get("mentions_offer") else ""))
        else:
            land_detail = "not checked yet (the next health run checks it)"
        items = [
            {"id": "sending_domain", "label": "Buy 1-2 sending domains (not digitalsolomon.com), create a mailbox on each, and publish SPF, DKIM and DMARC.",
             "done": bool(healthy),
             "detail": f"{len(healthy)} mailbox(es) pass SPF/DKIM/DMARC and the blacklist check"
                       + (f" (checked {checked[:16].replace('T', ' ')} UTC)" if checked else "")},
            {"id": "mailbox_secret", "label": "Paste each mailbox's JSON into Credentials as global/leadgen-mailbox-1, -2, ...",
             "done": bool(names), "detail": ", ".join(sorted(names)) or "none yet",
             "vault_slot": "global/leadgen-mailbox-1"},
            {"id": "notify_smtp", "label": "Optional: add global/leadgen-notify-smtp so notices come from operator@example.com.",
             "done": bool(inv.get("notify_smtp")), "optional": True, "vault_slot": "global/leadgen-notify-smtp"},
            {"id": "landing", "label": "Make digitalsolomon.com/audit live.", "done": bool(land.get("ok")),
             "detail": land_detail},
            {"id": "free_tiers", "label": "Optional: sign up for free tiers on the Connections page and paste each key into Credentials.",
             "done": not inv.get("recommended"), "optional": True,
             "detail": f"{len(inv.get('recommended', []))} recommended connection(s)"},
            {"id": "launch", "label": "Press Launch on the seed campaign.", "done": seed.get("status") not in (None, "draft"),
             "detail": f"seed campaign is {seed.get('status')}"},
        ]
        return {"items": items, "ready_to_send": bool(healthy) and seed.get("status") in ("testing", "active"),
                "mailboxes": [{"id": m["id"], "email": m.get("email"), "status": m.get("status"),
                               "health": m.get("health"), "error": m.get("error")} for m in pool_all],
                "inventory_at": inv.get("at")}

    # ------------------------------------------------------------------ monthly scout
    def maybe_scout(self, now: datetime | None = None) -> dict | None:
        now = now or utcnow()
        key = now.strftime("%Y-%m")
        if self.ledger.state("scout").get("month") == key:
            return None
        from .scout import scout
        try:
            res = scout(self, max_units=self.settings["scout"]["max_discovery_units"])
        except BudgetStop as exc:
            res = {"proposals": [], "note": str(exc)}
        self.ledger.set_state("scout", month=key, at=utcnow().isoformat(), **res)
        if res.get("proposals"):
            self.ledger.event("scout", f"Provider scout found {len(res['proposals'])} possible free tier(s).",
                              why="Monthly scout. Verify each before adding; nothing joins the rotation automatically.")
        return res


def make_engine(local: bool = False, state_path: str = ".state/leadgen.json", **kw) -> Engine:
    from .secrets import Secrets
    from .store import FirestoreStore, Ledger, LocalStore
    settings = load_settings()
    store = LocalStore(state_path) if local else FirestoreStore(settings["gcp_project"])
    secrets = Secrets(settings["gcp_project"], use_secret_manager=not local)
    return Engine(settings, Ledger(store), secrets, **kw)


_ = month_start
