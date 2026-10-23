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

from .enrich import build_evidence, lead_brief, owner_name, page_text, socials, tech_stack, years_in_business
from .extract import contact_page, read_site
from .llm import BudgetStop
from .providers import ProviderError
from .rotation import NoProviderAvailable
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


class Prospector:
    def __init__(self, engine):
        self.e = engine
        self.settings = engine.settings
        self.ledger = engine.ledger
        self.stats = {}

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
    def qualify(self, c: dict, cand, cell: dict, provider: str) -> tuple[str, dict | None]:
        d = cand.domain
        google = getattr(self.e.discover.registry.get(provider), "persist", "") == "place_id_only"
        maps = {} if google else {k: v for k, v in {"rating": cand.rating, "review_count": cand.review_count,
                                                    "categories": cand.categories, "hours": cand.hours}.items() if v}
        base = {"domain": d, "name": clean_name(cand.name, d), "website": cand.website, "segment": cell["segment"],
                "geo": cell["geo"], "cell_id": cell["id"], "discovered_by": provider,
                "found_at": utcnow().isoformat(), "place_id": cand.place_id or None, "maps": maps}
        if not google:
            base.update(phone=cand.phone, address=cand.address)
        # Google content (rating, reviews) is used in the moment for the brief only.
        transient = {"rating": cand.rating, "review_count": cand.review_count} if google else {}
        reviews = [] if google else list(cand.reviews)[:5]
        try:
            home, _ = self.e.scrape.call("fetch", homepage(cand.website))
        except (ProviderError, NoProviderAvailable) as exc:
            try:  # text reader: emails and owner names still work, absence observations won't be made
                home, _ = self.e.reader.call("fetch", homepage(cand.website))
            except (ProviderError, NoProviderAvailable):
                self.ledger.save_lead(c["id"], d, {**base, "status": "scrape_failed", "error": str(exc)[:200]})
                return "scrape_failed", None
        pages = [home]
        cp = contact_page(home)
        if cp and cp.rstrip("/") != home.url.rstrip("/"):
            try:
                page, _ = self.e.scrape.call("fetch", cp)
                pages.append(page)
            except ProviderError:
                pass
        site = read_site(pages)
        if site.harvest_notice:
            self.ledger.save_lead(c["id"], d, {**base, "status": "excluded", "reason": "site asks not to be emailed"})
            return "harvest_notice", None
        text = page_text(pages)
        lead = {**base, "facts": site.facts, "pages": site.pages, "tech": tech_stack(pages),
                "social": socials(pages), "years_in_business": years_in_business(text)}
        lead["owner"] = owner_name(self.e.llm, text, c["id"])
        email, source = site.best_email, (f"site:{site.pages[-1]}" if site.best_email else None)
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
        maps_for_brief = {**maps, **{k: v for k, v in transient.items() if v is not None}}
        evidence = build_evidence({**lead, "maps": maps_for_brief}, transient_reviews=reviews)
        brief = lead_brief(self.e.llm, c, lead, evidence)
        stored_ev = {k: v for k, v in evidence.items() if not (google and k.startswith(("maps:", "review:")))}
        lead.update(brief=brief, evidence=stored_ev, status="enriched",
                    reviews=reviews if reviews and not google else [])
        self.ledger.save_lead(c["id"], d, lead)
        lead["_evidence_full"] = evidence
        return "enriched", lead

    def people_email(self, domain: str) -> tuple[str | None, str | None]:
        if not self.e.people:
            return None, None
        try:
            persons, provider = self.e.people.call("find", domain)
        except (ProviderError, NoProviderAvailable):
            return None, None
        on_domain = [p for p in persons if p.email.split("@")[-1] == domain]
        on_domain.sort(key=lambda p: (not p.verified, -(p.confidence or 0)))
        if not on_domain:
            return None, None
        return on_domain[0].email.lower(), f"people:{provider}"

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
              "harvest_notice": 0, "email_rejected": 0, "skipped_known": 0, "scrape_failed": 0, "stopped": ""}
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
        seen = set()
        for cell_id, n in queries.items():
            cell = cells.get(cell_id)
            if not cell:
                continue
            for _ in range(n):
                if st["first_touches"] >= target:
                    return st
                try:
                    cands, provider = self.e.discover.call(
                        "search", cell["query"], cell["geo"], self.settings["discovery"]["candidates_per_query"],
                        kinds=c["icp"].get("prefer_kinds") or self.settings["discovery"]["prefer_kinds"])
                except NoProviderAvailable as exc:
                    st["stopped"] = str(exc)
                    return st
                except ProviderError as exc:
                    self.e.report["errors"].append(str(exc)[:300])
                    continue
                st["queries"] += 1
                for cand in cands:
                    if st["first_touches"] >= target:
                        return st
                    d = cand.domain
                    if not d or d in seen:
                        continue
                    seen.add(d)
                    st["candidates"] += 1
                    if self.excluded(c, cand):
                        continue
                    if self.ledger.lead(c["id"], d) or self.ledger.is_suppressed(domain=d) or \
                            self.ledger.domain_contacted_recently(d, c["settings"]["recontact_after_days"]):
                        st["skipped_known"] += 1
                        continue
                    try:
                        outcome, lead = self.qualify(c, cand, cell, provider)
                        st[outcome] = st.get(outcome, 0) + 1
                        if lead:
                            self.write_first_touch(c, lead)
                            st["first_touches"] += 1
                    except NoProviderAvailable as exc:
                        st["stopped"] = str(exc)
                        return st
                    except BudgetStop as exc:
                        st["stopped"] = str(exc)
                        return st
        return st

    def samples(self, c: dict, n: int = 3, max_queries: int | None = None) -> list[dict]:
        """n live drafts through the full writer path, for the New campaign review and preflight."""
        from .campaigns import campaign_link
        from .writer import LINK_TOKEN
        leads = [l for l in self.ledger.leads(c["id"], "enriched", "in_sequence") if l.get("evidence")][:n]
        tried, max_queries = 0, max_queries or max(3, n)
        for cell in c["icp"]["cells"]:
            if len(leads) >= n or tried >= max_queries:
                break
            tried += 1
            try:
                cands, provider = self.e.discover.call("search", cell["query"], cell["geo"], 10,
                                                       kinds=c["icp"].get("prefer_kinds") or ["maps", "web"])
            except (ProviderError, NoProviderAvailable) as exc:
                self.e.report["errors"].append(f"samples discovery: {exc}"[:300])
                continue
            for cand in cands:
                if len(leads) >= n:
                    break
                if self.excluded(c, cand) or self.ledger.lead(c["id"], cand.domain) \
                        or self.ledger.is_suppressed(domain=cand.domain):
                    continue
                try:
                    _, lead = self.qualify(c, cand, cell, provider)
                except NoProviderAvailable:
                    break
                if lead:
                    leads.append(lead)
        out = []
        for lead in leads[:n]:
            evidence = lead.pop("_evidence_full", None) or lead.get("evidence") or {}
            draft = self.e.writer.write(c, lead, evidence, lead.get("brief") or {}, step=0)
            link = campaign_link(c, lead["domain"], 0)
            out.append({"domain": lead["domain"], "name": lead["name"], "email_source": lead.get("email_source"),
                        "subject": draft["subject"], "body": draft["body"].replace(LINK_TOKEN, link),
                        "source": draft.get("source"), "tier": draft.get("tier"),
                        "passed": draft.get("source") != "template", "critic": draft.get("critic"),
                        "evidence_used": draft.get("evidence_used", [])})
        return out


def _keep(draft: dict) -> dict:
    return {k: draft.get(k) for k in ("subject", "body", "source", "tier", "facts_used", "evidence_used")} | \
        {"written_at": utcnow().isoformat()}
