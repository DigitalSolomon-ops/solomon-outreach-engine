"""Pilot: businesses with NO website, keyed by Google listing id, emails found on their own
BBB profile (default; Facebook and Yelp can be added, but they block our scrapers: the
2026-10-23 run found 0 and 1 emails there against 8 on BBB).

Measure first, scale later: the pilot runs on a fixed number of listings (50), stores what it
found in leadgen_pilot_nosite, and reports the email-find rate. Pilot leads never enter a
campaign or a sequence. No email found means the listing is dropped, never guessed: an address
counts only if it is printed on a profile page that names the business.

Only the listing id and the business name are kept from the maps provider (no rating, no
reviews, no phone from Google).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from .extract import choose_email, find_emails
from .providers import ProviderError
from .providers.base import registrable_domain
from .rotation import NoProviderAvailable, Rotation
from .store import utcnow

log = logging.getLogger(__name__)

COLLECTION = "pilot_nosite"
# Where to look, in order, and how to tell a business's own profile from a list page.
SOURCES = {
    "facebook": {"hosts": ("facebook.com",), "term": "facebook",
                 "skip": re.compile(r"/(?:groups|events|marketplace|search|share|sharer|login|watch|hashtag)\b", re.I)},
    "yelp": {"hosts": ("yelp.com",), "term": "yelp", "need": re.compile(r"/biz/", re.I),
             "skip": re.compile(r"/search\b", re.I)},
    "bbb": {"hosts": ("bbb.org",), "term": "bbb", "need": re.compile(r"/profile/", re.I),
            "skip": re.compile(r"/search\b", re.I)},
}
DEFAULT_SOURCES = ["bbb"]
PLATFORM_DOMAINS = {"facebook.com", "fb.com", "facebookmail.com", "meta.com", "yelp.com", "bbb.org",
                    "council.bbb.org", "google.com"}
STOP = {"llc", "inc", "co", "company", "the", "and", "of", "roofing", "roofers", "roof", "contractor",
        "contractors", "services", "service", "construction", "group", "corp", "&"}


def name_tokens(name: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 1]
    core = [w for w in words if w not in STOP]
    return core or words


def names_match(name: str, text: str, need: float = 0.6) -> bool:
    toks = name_tokens(name)
    if not toks:
        return False
    low = text.lower()
    return sum(1 for t in toks if re.search(rf"\b{re.escape(t)}\b", low)) / len(toks) >= need


def profile_url(source: str, url: str) -> bool:
    cfg = SOURCES[source]
    host = registrable_domain(url)
    if not any(host == h or host.endswith("." + h) for h in cfg["hosts"]):
        return False
    path = urlparse(url if "://" in url else "https://" + url).path
    if cfg["skip"].search(path) or path.strip("/") == "":
        return False
    return bool(cfg["need"].search(path)) if cfg.get("need") else True


def business_email(page, name: str) -> str | None:
    """An address printed on the page, not the platform's own, on a page that names the business."""
    text = (page.markdown or "") + "\n" + re.sub(r"<[^>]+>", " ", page.html or "")
    if not names_match(name, text):
        return None
    emails = [e for e in find_emails(page) if e.split("@")[-1] not in PLATFORM_DOMAINS
              and not any(e.split("@")[-1].endswith("." + d) for d in PLATFORM_DOMAINS)]
    return choose_email(emails, "")


class NoSitePilot:
    def __init__(self, engine, sources: list[str] | None = None):
        self.e = engine
        self.sources = [s for s in (sources or DEFAULT_SOURCES) if s in SOURCES]
        self.ledger = engine.ledger
        d = engine.discover
        # Only maps adapters that can return no-website listings with a Google listing id.
        self.maps = Rotation("discovery", [x for x in d.entries if x.get("kind") == "maps" and
                                           hasattr(d.registry.get(x["name"]), "search_nosite")],
                             d.registry, d.secrets, d.ledger, on_exhausted=d.on_exhausted)

    # ------------------------------------------------------------------ listings
    def listings(self, query: str, geos: list[str], n: int) -> tuple[list[dict], list[str]]:
        out, seen, notes = [], set(), []
        for geo in geos:
            if len(out) >= n:
                break
            try:
                cands, provider = self.maps.call("search_nosite", query, geo, 20)
            except NoProviderAvailable as exc:
                notes.append(f"maps: {exc}")
                break
            except ProviderError as exc:
                notes.append(f"maps {geo}: {exc}"[:200])
                continue
            for cand in cands:
                pid = (cand.place_id or "").strip()
                if not pid or pid in seen or not cand.name.strip():
                    continue
                seen.add(pid)
                if self.ledger.s.get(COLLECTION, f"gbp_{pid}"):
                    continue                    # already in an earlier pilot run
                out.append({"place_id": pid, "name": cand.name.strip()[:100], "geo": geo, "provider": provider})
                if len(out) >= n:
                    break
        return out, notes

    # ------------------------------------------------------------------ one listing
    def find_profile(self, listing: dict, source: str) -> str | None:
        try:
            results, _ = self.e.discover.call("search", f"{listing['name']} {SOURCES[source]['term']}",
                                              listing["geo"], 5, kinds=["web"])
        except (ProviderError, NoProviderAvailable):
            return None
        for r in results:
            if profile_url(source, r.website) and names_match(listing["name"], r.name + " " + r.website.replace("-", " ")):
                return r.website
        return None

    def fetch(self, url: str):
        for rot in (self.e.scrape, self.e.reader):
            try:
                page, _ = rot.call("fetch", url)
                return page
            except (ProviderError, NoProviderAvailable):
                continue
        return None

    def run_one(self, listing: dict, prospector) -> dict:
        doc = {**listing, "profiles": {}, "email": None, "email_source": None, "verdict": None,
               "status": "not_found", "at": utcnow().isoformat()}
        for source in self.sources:
            url = self.find_profile(listing, source)
            doc["profiles"][source] = url
            if not url:
                continue
            page = self.fetch(url)
            email = business_email(page, listing["name"]) if page else None
            if email:
                if self.ledger.is_suppressed(email=email):
                    doc["status"] = "suppressed"
                    break
                verdict, by = prospector.verify(email)
                doc.update(email=email, email_source=f"{source}:{url}", verdict=verdict, verified_by=by,
                           status="found")
                break
        self.ledger.s.set(COLLECTION, f"gbp_{listing['place_id']}", doc, merge=False)
        return doc

    # ------------------------------------------------------------------ the run
    def run(self, query: str, geos: list[str], n: int = 50) -> dict:
        from .prospect import Prospector
        started = utcnow()
        listings, notes = self.listings(query, geos, n)
        pr = Prospector(self.e)
        docs = [self.run_one(x, pr) for x in listings]
        rep = report(docs, query=query, geos=geos, target=n, notes=notes, sources=self.sources)
        rep["started_at"], rep["finished_at"] = started.isoformat(), utcnow().isoformat()
        self.ledger.event("pilot_nosite", f"No-website pilot: {rep['emails_found']}/{rep['listings']} emails found.",
                          why="Pilot on businesses without a website; measured before scaling.",
                          cost_impact="free provider allowances only", detail=to_markdown(rep))
        return rep


def report(docs: list[dict], *, query: str, geos: list[str], target: int, notes: list[str] | None = None,
           sources: list[str] | None = None) -> dict:
    n = len(docs)
    found = [d for d in docs if d["status"] == "found"]
    ok = [d for d in found if d.get("verdict") == "ok"]
    by_source = {s: {"profile_found": sum(1 for d in docs if d["profiles"].get(s)),
                     "email_found": sum(1 for d in found if (d["email_source"] or "").startswith(s + ":"))}
                 for s in (sources or DEFAULT_SOURCES)}
    return {"query": query, "geos": geos, "target": target, "listings": n, "emails_found": len(found),
            "emails_verified_ok": len(ok), "find_rate": round(len(found) / n, 3) if n else 0.0,
            "usable_rate": round(len(ok) / n, 3) if n else 0.0, "by_source": by_source,
            "dropped_no_email": n - len(found), "notes": notes or []}


def to_markdown(rep: dict) -> str:
    lines = [f"# No-website pilot: {rep['query']}", "",
             f"Geos: {', '.join(rep['geos'])}. Target {rep['target']} listings, got {rep['listings']}.", "",
             "| Measure | Count | Rate |", "|---|---|---|",
             f"| Listings (no website, Google listing id) | {rep['listings']} | |",
             f"| Email found on an own profile page | {rep['emails_found']} | {rep['find_rate']:.0%} |",
             f"| ...and verified ok | {rep['emails_verified_ok']} | {rep['usable_rate']:.0%} |",
             f"| Dropped, no email (never guessed) | {rep['dropped_no_email']} | |", "",
             "| Source | Profile found | Email found |", "|---|---|---|"]
    lines += [f"| {s} | {v['profile_found']} | {v['email_found']} |" for s, v in rep["by_source"].items()]
    if rep.get("notes"):
        lines += ["", "Notes:"] + [f"- {x}" for x in rep["notes"]]
    return "\n".join(lines) + "\n"


def write_report(rep: dict, out_dir: str | Path) -> Path:
    path = Path(out_dir) / f"nosite-pilot-{date.today().isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(rep), encoding="utf-8")
    return path
