"""Find, qualify and prepare leads for one campaign; write the day's first touches and due follow-ups.

Per candidate: own-site read (raw HTML for observations, Jina-style text for emails and the
about page), enrichment v2, the email to use (published on the business's own site, or from a
connected people-data provider AND verified ok; never a guessed pattern), one verification
cached on the lead, the cited lead brief, then the first touch.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from . import strategies
from .enrich import build_evidence, lead_brief, owner_name, page_text, socials, tech_stack, years_in_business
from .extract import contact_page, read_site
from .llm import BudgetStop
from .providers import ProviderError
from .rotation import NoProviderAvailable
from .sitequality import assess
from .strategies.base import SiteRead
from .sender import due_date, today_et
from .store import utcnow
from .writer import SkipEmail

log = logging.getLogger(__name__)


def homepage(url: str) -> str:
    from urllib.parse import urlparse
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def clean_name(name: str, domain: str) -> str:
    """Pick the business name out of a search title ('Acme HVAC | Heating in Gastonia')."""
    parts = [p.strip() for p in re.split(r"\s[|–—-]\s|:\s", name) if p.strip()]
    if not parts:
        return domain
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())

    def score(part: str) -> float:
        words = [w for w in re.findall(r"[a-z0-9]+", part.lower()) if len(w) > 2]
        return sum(w in stem for w in words) / len(words) if words else 0.0

    best = max(parts, key=score) if len(parts) > 1 else parts[0]
    if len(parts) > 1 and score(best) == 0:
        best = parts[0]
    return best if 2 <= len(best) <= 60 else domain


UNASSESSED_RETRY_DAYS = 7      # a site we could only read as text is re-checked after this
NOSITE_PREFIX = "gbp-"         # lead key for a business with no website: gbp-<Google listing id>
NOSITE_SAMPLE_LISTINGS = 40    # most no-website listings one samples/preflight call may try


def nosite_key(place_id: str) -> str:
    return NOSITE_PREFIX + place_id


def nosite_fact(name: str) -> dict:
    return {"id": "nosite", "pillar": "Presence",
            "text": f"When I searched for {name} online I couldn't find a website for the business, "
                    "only directory listings."}


class Prospector:
    def __init__(self, engine):
        self.e = engine
        self.settings = engine.settings
        self.ledger = engine.ledger
        self.stats = {}
        self._nosite = None
        self.probe = False             # strategy-probe: count only, no brief, no owner call, no writing
        self.enrichment_calls = 0

    @staticmethod
    def strategy(c: dict):
        return strategies.for_campaign(c)

    def weak_only(self, c: dict) -> bool:
        return self.strategy(c).reads_quality

    def wants_nosite(self, c: dict) -> bool:
        return "nosite" in self.strategy(c).passes

    def nosite_only(self, c: dict) -> bool:
        """No website pass at all (new_business)."""
        return "site" not in self.strategy(c).passes

    def known(self, c: dict, key: str) -> bool:
        """A lead already on file is skipped, except a site we could only read as text, which is
        re-checked once its retry date passes (a weak-site verdict needs the raw page)."""
        lead = self.ledger.lead(c["id"], key)
        if not lead:
            return False
        if lead.get("status") == "site_unassessed":
            return (lead.get("retry_after") or "9") > utcnow().isoformat()
        return True

    # ------------------------------------------------------------------ filters
    def excluded(self, c: dict, cand) -> str | None:
        d = cand.domain
        if not d:
            return "no website"
        if any(d == x or d.endswith("." + x) for x in c["icp"].get("exclude_domains", [])):
            return "excluded domain"
        if d.endswith((".gov", ".edu", ".mil")):
            return "public body"
        if any(k.lower() in cand.name.lower() for k in c["icp"].get("exclude_name_keywords", [])):
            return "excluded name"
        return None

    # ------------------------------------------------------------------ verification
    def verify(self, email: str) -> tuple[str, str]:
        """Real verifiers first (recurring before one-time). mx_only only when all are spent."""
        try:
            verdict, by = self.e.verify.call("verify", email)
            return verdict, by
        except NoProviderAvailable:
            pass
        except ProviderError:
            return "unknown", "none"
        if not self.settings["send"].get("mx_only_fallback"):
            return "unknown", "none"
        from .providers.verify import MxCheck
        try:
            verdict, _ = MxCheck().verify(email)
        except ProviderError:
            return "unknown", "none"
        return verdict, "mx_check"

    # ------------------------------------------------------------------ one candidate
    def qualify(self, c: dict, cand, cell: dict, provider: str, strat=None) -> tuple[str, dict | None]:
        strat = strat or self.strategy(c)
        d = cand.domain
        google = getattr(self.e.discover.registry.get(provider), "persist", "") == "place_id_only"
        maps = {} if google else {k: v for k, v in {"rating": cand.rating, "review_count": cand.review_count,
                                                    "categories": cand.categories, "hours": cand.hours}.items() if v}
        base = {"domain": d, "name": clean_name(cand.name, d), "website": cand.website, "segment": cell["segment"],
                "geo": cell["geo"], "cell_id": cell["id"], "discovered_by": provider,
                "found_at": utcnow().isoformat(), "place_id": cand.place_id or None, "maps": maps,
                "strategy": strat.id}
        if not google:
            base.update(phone=cand.phone, address=cand.address)
        # Google content (rating, reviews) is used in the moment for the brief only.
        transient = {"rating": cand.rating, "review_count": cand.review_count} if google else {}
        reviews = [] if google else list(cand.reviews)[:5]
        weak_only = strat.reads_quality
        try:
            home, _ = self.e.scrape.call("fetch", homepage(cand.website))
        except (ProviderError, NoProviderAvailable) as exc:
            try:  # text reader: emails and owner names still work, absence observations won't be made
                home, _ = self.e.reader.call("fetch", homepage(cand.website))
            except (ProviderError, NoProviderAvailable):
                home = None
            if home is None and not strat.keeps_broken_sites:
                self.ledger.save_lead(c["id"], d, {**base, "status": "scrape_failed", "error": str(exc)[:200]})
                return "scrape_failed", None
        pages = [home] if home else []
        cp = contact_page(home) if home else None
        if cp and cp.rstrip("/") != home.url.rstrip("/"):
            try:
                page, _ = self.e.scrape.call("fetch", cp)
                pages.append(page)
            except ProviderError:
                pass
        quality = assess(pages, fetch_failed=home is None) if weak_only else {}
        site = read_site(pages) if pages else None
        read = SiteRead(pages=pages, quality=quality, site=site)
        drop = strat.drop_site(self, c, cand, read)
        if drop:
            row = {**base, "status": drop.status, "reason": drop.reason,
                   **({"site_quality": quality} if weak_only else {})}
            if drop.retry:
                row["retry_after"] = (utcnow() + timedelta(days=UNASSESSED_RETRY_DAYS)).isoformat()
            self.ledger.save_lead(c["id"], d, row)
            return drop.outcome, None
        if site and site.harvest_notice:
            self.ledger.save_lead(c["id"], d, {**base, "status": "excluded", "reason": "site asks not to be emailed"})
            return "harvest_notice", None
        text = page_text(pages) if pages else ""
        site_facts = [f for f in (site.facts if site else []) + strat.facts(self, c, cand, read)
                      if strategies.allows_fact(strat.entry, f["id"])]
        lead = {**base, "facts": site_facts, "pages": site.pages if site else [], "tech": tech_stack(pages),
                "social": socials(pages), "years_in_business": years_in_business(text),
                **({"site_quality": quality} if weak_only else {})}
        lead["owner"] = self.owner(c, text)
        email = site.best_email if site else None
        source = f"site:{site.pages[-1]}" if email else None
        if not email:
            email, source = self.people_email(d)
        if not email:
            self.ledger.save_lead(c["id"], d, {**lead, "status": "no_email"})
            return "no_email", None
        if self.ledger.is_suppressed(email=email):
            self.ledger.save_lead(c["id"], d, {**lead, "status": "suppressed"})
            return "skipped_known", None
        verdict, verifier = self.verify(email)
        lead.update(email=email, email_source=source, verdict=verdict, verified_by=verifier,
                    verified_at=utcnow().isoformat())
        accepted = set(self.settings["send"]["accept_verdicts"])
        if verifier == "mx_check" and source.startswith("site:"):
            accepted.add("mx_only")  # only reached when every verifier is spent for the period
        if verdict not in accepted:
            self.ledger.save_lead(c["id"], d, {**lead, "status": "email_rejected"})
            return "email_rejected", None
        if self.probe:
            self.ledger.save_lead(c["id"], d, {**lead, "status": "verified"})
            return "verified", lead
        maps_for_brief = {**maps, **{k: v for k, v in transient.items() if v is not None}}
        evidence = strategies.filter_evidence(
            strat.entry, build_evidence({**lead, "maps": maps_for_brief}, transient_reviews=reviews))
        brief = lead_brief(self.e.llm, c, lead, evidence)
        stored_ev = {k: v for k, v in evidence.items() if not (google and k.startswith(("maps:", "review:")))}
        lead.update(brief=brief, evidence=stored_ev, status="enriched",
                    reviews=reviews if reviews and not google and strat.entry["cites_site_record"] else [])
        self.ledger.save_lead(c["id"], d, lead)
        lead["_evidence_full"] = evidence
        return "enriched", lead

    def owner(self, c: dict, text: str):
        """The owner's first name from page text (a Haiku call). Skipped by probes, which only count."""
        if self.probe or not text:
            return None
        return owner_name(self.e.llm, text, c["id"])

    def people_email(self, domain: str) -> tuple[str | None, str | None]:
        if not self.e.people:
            return None, None
        self.enrichment_calls += 1
        try:
            persons, provider = self.e.people.call("find", domain)
        except (ProviderError, NoProviderAvailable):
            return None, None
        on_domain = [p for p in persons if p.email.split("@")[-1] == domain]
        on_domain.sort(key=lambda p: (not p.verified, -(p.confidence or 0)))
        if not on_domain:
            return None, None
        return on_domain[0].email.lower(), f"people:{provider}"

    # ------------------------------------------------------------------ no-website leads
    def nosite(self):
        """The pilot's machinery (maps listings without a site, BBB profile, printed email)."""
        if self._nosite is None:
            from .pilot_nosite import NoSitePilot
            self._nosite = NoSitePilot(self.e)
        return self._nosite

    def nosite_listings(self, c: dict, cell: dict, limit: int) -> list[dict]:
        """No-website listings for one cell that this campaign hasn't looked at yet."""
        pilot = self.nosite()
        strat = self.strategy(c)
        if not pilot.maps.entries:
            return []
        cached = self._nosite_cache(strat, cell) if strat.nosite_depth else None
        if cached is not None:
            return [x for x in cached if not self.known(c, nosite_key(x["place_id"]))]
        try:
            cands, provider = pilot.maps.call("search_nosite", cell["query"], cell["geo"], max(limit, strat.nosite_depth))
        except NoProviderAvailable:
            return []
        except ProviderError as exc:
            self.e.report["errors"].append(f"nosite maps {cell['geo']}: {exc}"[:300])
            return []
        passed = []
        for cand in cands:
            pid = (cand.place_id or "").strip()
            if not pid or not cand.name.strip() or strat.drop_nosite_listing(self, c, cand):
                continue
            passed.append({"place_id": pid, "name": cand.name.strip()[:100], "geo": cell["geo"], "provider": provider})
        if strat.nosite_depth:
            self._nosite_cache(strat, cell, passed)
        return [x for x in passed if not self.known(c, nosite_key(x["place_id"]))]

    NOSITE_CACHE_HOURS = 24

    def _nosite_cache(self, strat, cell: dict, listings: list | None = None):
        """A deep no-website search (3 Google requests) is reused for a day: a waiting campaign
        retries preflight hourly and must not spend the shared Places allowance each time. Only
        listings that passed the filter are kept, as listing id and name (no counts or ratings)."""
        import hashlib
        import json as _json
        raw = _json.dumps([strat.id, strat.params, cell["query"], cell["geo"], strat.nosite_depth], sort_keys=True)
        key = "nosite_cache_" + hashlib.sha1(raw.encode()).hexdigest()[:16]
        if listings is not None:
            self.ledger.set_state(key, at=utcnow().isoformat(), listings=listings)
            return listings
        st = self.ledger.state(key)
        if st.get("at") and st["at"] > (utcnow() - timedelta(hours=self.NOSITE_CACHE_HOURS)).isoformat():
            return list(st.get("listings") or [])
        return None

    def qualify_nosite(self, c: dict, listing: dict, cell: dict) -> tuple[str, dict | None]:
        """A business with no website. Kept from the maps listing: its id and name only. The email
        must be printed on the business's own BBB profile page (pilot rule): never guessed."""
        from .pilot_nosite import business_email
        key = nosite_key(listing["place_id"])
        pilot = self.nosite()
        base = {"domain": key, "name": listing["name"], "website": "", "nosite": True,
                "segment": cell["segment"], "geo": cell["geo"], "cell_id": cell["id"],
                "discovered_by": listing["provider"], "found_at": utcnow().isoformat(),
                "place_id": listing["place_id"], "maps": {}, "strategy": self.strategy(c).id}
        email = source = page = None
        for src in pilot.sources:
            url = pilot.find_profile(listing, src)
            if not url:
                continue
            page = pilot.fetch(url)
            email = business_email(page, listing["name"]) if page else None
            if email:
                source = f"{src}:{url}"
                break
        if not email:
            self.ledger.save_lead(c["id"], key, {**base, "status": "no_email"})
            return "no_email", None
        if self.ledger.is_suppressed(email=email):
            self.ledger.save_lead(c["id"], key, {**base, "status": "suppressed"})
            return "skipped_known", None
        text = page_text([page]) if page else ""
        lead = {**base, "facts": [nosite_fact(listing["name"])], "pages": [], "tech": [], "social": {},
                "years_in_business": years_in_business(text) if text else None}
        lead["owner"] = self.owner(c, text)
        verdict, verifier = self.verify(email)
        lead.update(email=email, email_source=source, verdict=verdict, verified_by=verifier,
                    verified_at=utcnow().isoformat())
        if verdict not in set(self.settings["send"]["accept_verdicts"]):
            self.ledger.save_lead(c["id"], key, {**lead, "status": "email_rejected"})
            return "email_rejected", None
        if self.probe:
            self.ledger.save_lead(c["id"], key, {**lead, "status": "verified"})
            return "verified", lead
        evidence = strategies.filter_evidence(self.strategy(c).entry, build_evidence(lead))
        brief = lead_brief(self.e.llm, c, lead, evidence)
        lead.update(brief=brief, evidence=evidence, status="enriched")
        self.ledger.save_lead(c["id"], key, lead)
        lead["_evidence_full"] = evidence
        return "enriched", lead

    # ------------------------------------------------------------------ leads from a strategy's own records
    def finish_lead(self, c: dict, strat, key: str, base: dict, email: str, source: str,
                    text: str = "") -> tuple[str, dict | None]:
        """A record that already carries its own email (FMCSA): suppression, one verification,
        then the brief. Nothing is looked up to fill a gap, and nothing is guessed."""
        if self.ledger.is_suppressed(email=email):
            self.ledger.save_lead(c["id"], key, {**base, "status": "suppressed"})
            return "skipped_known", None
        lead = {**base, "pages": [], "tech": [], "social": {}, "years_in_business": None,
                "owner": self.owner(c, text)}
        verdict, verifier = self.verify(email)
        lead.update(email=email, email_source=source, verdict=verdict, verified_by=verifier,
                    verified_at=utcnow().isoformat())
        if verdict not in set(self.settings["send"]["accept_verdicts"]):
            self.ledger.save_lead(c["id"], key, {**lead, "status": "email_rejected"})
            return "email_rejected", None
        if self.probe:
            self.ledger.save_lead(c["id"], key, {**lead, "status": "verified"})
            return "verified", lead
        evidence = strategies.filter_evidence(strat.entry, build_evidence(lead))
        brief = lead_brief(self.e.llm, c, lead, evidence)
        lead.update(brief=brief, evidence=evidence, status="enriched")
        self.ledger.save_lead(c["id"], key, lead)
        lead["_evidence_full"] = evidence
        return "enriched", lead

    # ------------------------------------------------------------------ writing
    def write_first_touch(self, c: dict, lead: dict) -> bool:
        evidence = lead.pop("_evidence_full", None) or lead.get("evidence") or {}
        draft = self.e.writer.write(c, lead, evidence, lead.get("brief") or {}, step=0)
        today = today_et()
        self.ledger.save_lead(c["id"], lead["domain"], {
            "status": "in_sequence",
            "seq": {"next_step": 0, "next_due": today.isoformat(), "drafts": {"0": _keep(draft)},
                    "sent_steps": []}})
        return True

    def write_followups(self, c: dict) -> dict:
        out = {"written": 0, "skipped": 0}
        horizon = (today_et() + timedelta(days=1)).isoformat()
        for lead in self.ledger.leads(c["id"], "in_sequence"):
            seq = lead.get("seq") or {}
            step = seq.get("next_step")
            if not step or str(step) in (seq.get("drafts") or {}) or str(seq.get("next_due", "9")) > horizon:
                continue
            thread = (self.ledger.thread(seq.get("thread_id")) or {}).get("messages", []) if seq.get("thread_id") else []
            prior = [{"step": m["step"], "subject": m["subject"], "body": m["body"]}
                     for m in thread if m.get("dir") == "out" and m.get("kind") == "sequence"]
            if not prior:
                continue
            try:
                draft = self.e.writer.write(c, lead, lead.get("evidence") or {}, lead.get("brief") or {},
                                            step=step, thread=prior)
            except SkipEmail as exc:
                nxt = step + 1
                steps = {s["step"]: s for s in c["sequence"]}
                upd = {**seq, "skipped_steps": sorted(set(seq.get("skipped_steps", [])) | {step})}
                if nxt in steps and seq.get("first_touch_date"):
                    from datetime import date
                    upd.update(next_step=nxt, next_due=due_date(date.fromisoformat(seq["first_touch_date"]),
                                                                steps[nxt]["day"]).isoformat())
                else:
                    upd.update(next_step=None, next_due=None)
                self.ledger.save_lead(c["id"], lead["domain"], {"seq": upd,
                                      **({} if upd["next_step"] is not None else {"status": "sequence_done"})})
                self.ledger.event("followup_skipped", f"Skipped step {step + 1} for {lead['domain']}.",
                                  why=str(exc)[:200], campaign_id=c["id"])
                out["skipped"] += 1
                continue
            drafts = {**(seq.get("drafts") or {}), str(step): _keep(draft)}
            self.ledger.save_lead(c["id"], lead["domain"], {"seq": {**seq, "drafts": drafts}})
            out["written"] += 1
        return out

    # ------------------------------------------------------------------ the day's run
    def run_campaign(self, c: dict, queries: dict[str, int], target: int) -> dict:
        st = {"queries": 0, "candidates": 0, "enriched": 0, "first_touches": 0, "no_email": 0,
              "harvest_notice": 0, "email_rejected": 0, "skipped_known": 0, "scrape_failed": 0,
              "site_not_weak": 0, "site_unassessed": 0, "nosite_listings": 0, "strategy_dropped": 0,
              "stopped": ""}
        strat = self.strategy(c)
        st["strategy"] = strat.id
        cells = {x["id"]: x for x in c["icp"]["cells"]}
        # Leads enriched earlier (samples, referrals) go first.
        ready = [l for l in self.ledger.leads(c["id"], "enriched", "referral_queued")]
        for lead in ready:
            if st["first_touches"] >= target:
                break
            try:
                if lead["status"] == "referral_queued":
                    verdict, by = self.verify(lead["email"])
                    if verdict != "ok":
                        self.ledger.save_lead(c["id"], lead["domain"], {"status": "email_rejected", "verdict": verdict})
                        continue
                    lead.update(verdict=verdict, verified_by=by)
                self.write_first_touch(c, lead)
                st["first_touches"] += 1
            except BudgetStop as exc:
                st["stopped"] = str(exc)
                return st
        passes = {"site": self._site_pass, "nosite": self._nosite_pass, "source": self._source_pass}
        for name in strat.passes:
            if passes[name](c, strat, cells, queries, target, st):
                return st
        return st

    def _take(self, c: dict, st: dict, outcome: str, lead: dict | None, prefix: str = "") -> None:
        st[prefix + outcome] = st.get(prefix + outcome, 0) + 1
        if lead:
            self.write_first_touch(c, lead)
            st["first_touches"] += 1

    def _site_pass(self, c, strat, cells, queries, target, st) -> bool:
        """Candidates with a website. True = stop the run (target reached or a stage is out)."""
        seen = set()
        for cell_id, n in queries.items():
            cell = cells.get(cell_id)
            if not cell:
                continue
            for _ in range(n):
                if st["first_touches"] >= target:
                    return True
                try:
                    cands, provider = strat.find(self, c, cell, self.settings["discovery"]["candidates_per_query"])
                except NoProviderAvailable as exc:
                    st["stopped"] = str(exc)
                    return True
                except ProviderError as exc:
                    self.e.report["errors"].append(str(exc)[:300])
                    continue
                st["queries"] += 1
                for cand in cands:
                    if st["first_touches"] >= target:
                        return True
                    d = cand.domain
                    if not d or d in seen:
                        continue
                    seen.add(d)
                    st["candidates"] += 1
                    if self.excluded(c, cand):
                        continue
                    if self.known(c, d) or self.ledger.is_suppressed(domain=d) or \
                            self.ledger.domain_contacted_recently(d, c["settings"]["recontact_after_days"]):
                        st["skipped_known"] += 1
                        continue
                    try:
                        if self.drop_listing(c, strat, cand, cell, provider):
                            st["strategy_dropped"] += 1
                            continue
                        outcome, lead = self.qualify(c, cand, cell, provider, strat)
                        self._take(c, st, outcome, lead)
                    except (NoProviderAvailable, BudgetStop) as exc:
                        st["stopped"] = str(exc)
                        return True
        return False

    def drop_listing(self, c: dict, strat, cand, cell: dict, provider: str) -> bool:
        """The strategy's listing filter. A drop is recorded so the listing isn't checked again."""
        try:
            reason = strat.drop_listing(self, c, cand)
        except ProviderError as exc:
            self.e.report["errors"].append(f"{strat.id} {cand.domain}: {exc}"[:300])
            return True
        if reason:
            self.ledger.save_lead(c["id"], cand.domain, {
                "domain": cand.domain, "name": clean_name(cand.name, cand.domain), "segment": cell["segment"],
                "geo": cell["geo"], "cell_id": cell["id"], "discovered_by": provider, "strategy": strat.id,
                "found_at": utcnow().isoformat(), "status": "excluded", "reason": reason})
            return True
        return False

    def _nosite_pass(self, c, strat, cells, queries, target, st) -> bool:
        """Businesses with no website at all (Website Launch only), after any site pass."""
        for cell_id in queries:
            cell = cells.get(cell_id)
            if not cell or st["first_touches"] >= target:
                continue
            for listing in self.nosite_listings(c, cell, self.settings["discovery"]["candidates_per_query"]):
                if st["first_touches"] >= target:
                    break
                st["nosite_listings"] += 1
                try:
                    outcome, lead = self.qualify_nosite(c, listing, cell)
                    self._take(c, st, outcome, lead, prefix="nosite_")
                except BudgetStop as exc:
                    st["stopped"] = str(exc)
                    return True
        return False

    def source_items(self, c: dict, strat, cell: dict, limit: int) -> list:
        """The strategy's own records for one cell, minus ones this campaign already has."""
        items, _ = strat.source_items(self, c, cell, limit)
        return [i for i in items if not self.known(c, i.key)]

    def _source_pass(self, c, strat, cells, queries, target, st) -> bool:
        """Records from the strategy's own source (FMCSA, SAM.gov)."""
        for cell_id in queries:
            cell = cells.get(cell_id)
            if not cell or st["first_touches"] >= target:
                continue
            try:
                items = self.source_items(c, strat, cell, self.settings["discovery"]["candidates_per_query"])
            except NoProviderAvailable as exc:
                st["stopped"] = str(exc)
                return True
            except ProviderError as exc:
                self.e.report["errors"].append(f"{strat.id} {cell['geo']}: {exc}"[:300])
                continue
            st["queries"] += 1
            for item in items:
                if st["first_touches"] >= target:
                    return True
                st["candidates"] += 1
                try:
                    outcome, lead = strat.qualify_source(self, c, item, cell)
                    self._take(c, st, outcome, lead)
                except (NoProviderAvailable, BudgetStop) as exc:
                    st["stopped"] = str(exc)
                    return True
        return False

    def samples(self, c: dict, n: int = 3, max_queries: int | None = None, progress=None) -> list[dict]:
        """n live drafts through the full writer path, for the New campaign review and preflight.

        progress(stage, done, total) is called as leads are found and drafts written (preflight
        shows it in the dashboard)."""
        from .campaigns import campaign_link
        from .writer import LINK_TOKEN
        say = progress or (lambda *a: None)
        leads = [l for l in self.ledger.leads(c["id"], "enriched", "in_sequence") if l.get("evidence")][:n]
        say("Finding businesses to write to", len(leads), n)
        tried, max_queries = 0, max_queries or max(3, n)
        strat = self.strategy(c)
        for cell in (c["icp"]["cells"] if "site" in strat.passes else []):
            if len(leads) >= n or tried >= max_queries:
                break
            tried += 1
            try:
                cands, provider = strat.sample_find(self, c, cell, 10)
            except (ProviderError, NoProviderAvailable) as exc:
                self.e.report["errors"].append(f"samples discovery: {exc}"[:300])
                continue
            for cand in cands:
                if len(leads) >= n:
                    break
                if self.excluded(c, cand) or self.known(c, cand.domain)                         or self.ledger.is_suppressed(domain=cand.domain):
                    continue
                try:
                    if self.drop_listing(c, strat, cand, cell, provider):
                        continue
                    _, lead = self.qualify(c, cand, cell, provider, strat)
                except NoProviderAvailable:
                    break
                if lead:
                    leads.append(lead)
                    say("Finding businesses to write to", len(leads), n)
        for cell in (c["icp"]["cells"][:max_queries] if "source" in strat.passes else []):
            if len(leads) >= n:
                break
            try:
                items = self.source_items(c, strat, cell, 20)
            except (ProviderError, NoProviderAvailable) as exc:
                self.e.report["errors"].append(f"samples {strat.id}: {exc}"[:300])
                continue
            for item in items:
                if len(leads) >= n:
                    break
                try:
                    _, lead = strat.qualify_source(self, c, item, cell)
                except NoProviderAvailable:
                    break
                if lead:
                    leads.append(lead)
                    say("Finding businesses to write to", len(leads), n)
        if self.wants_nosite(c):
            # About 1 in 5 no-website listings has an email on its BBB page (2026-10-23 pilot),
            # so cap the listings tried per call: each costs a web search and a page fetch.
            budget = NOSITE_SAMPLE_LISTINGS
            for cell in c["icp"]["cells"][:max_queries]:
                if len(leads) >= n or budget <= 0:
                    break
                for listing in self.nosite_listings(c, cell, 20):
                    if len(leads) >= n or budget <= 0:
                        break
                    budget -= 1
                    _, lead = self.qualify_nosite(c, listing, cell)
                    if lead:
                        leads.append(lead)
                        say("Finding businesses to write to (including ones with no website)", len(leads), n)
        out = []
        for i, lead in enumerate(leads[:n]):
            say("Writing sample emails", i, len(leads[:n]))
            evidence = lead.pop("_evidence_full", None) or lead.get("evidence") or {}
            draft = self.e.writer.write(c, lead, evidence, lead.get("brief") or {}, step=0)
            link = campaign_link(c, lead["domain"], 0)
            out.append({"domain": lead["domain"], "name": lead["name"], "email_source": lead.get("email_source"),
                        "subject": draft["subject"], "body": draft["body"].replace(LINK_TOKEN, link),
                        "source": draft.get("source"), "tier": draft.get("tier"),
                        "passed": draft.get("source") != "template", "critic": draft.get("critic"),
                        "evidence_used": draft.get("evidence_used", []),
                        # why each model attempt was rejected, so review and preflight show the reason
                        "attempts": draft.get("attempts") or []})
        return out


def _keep(draft: dict) -> dict:
    return {k: draft.get(k) for k in ("subject", "body", "source", "tier", "facts_used", "evidence_used")} | \
        {"written_at": utcnow().isoformat()}
