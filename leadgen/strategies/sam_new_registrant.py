"""#6 sam_new_registrant: small businesses that registered in SAM.gov recently.

Source: SAM.gov Entity Management API v3, public data (SAM.gov account key). Public data has no
point-of-contact email, so a lead needs the website the entity listed in SAM.gov; the address
then comes from that site through the normal domain flow (published on the site, or a people
provider plus verification). An entity with no website listed is dropped, never guessed.
"""
from __future__ import annotations

from datetime import date

from ..providers import Candidate
from ..providers.base import registrable_domain
from ..store import utcnow
from ._util import long_date, state_of, tidy_name
from .base import SourceItem, Strategy

MAX_PAGES = 5
PAGE_SIZE = 10


class SamNewRegistrant(Strategy):
    passes = ("source",)

    def __init__(self, entry, params):
        super().__init__(entry, params)
        self._page: dict[str, int] = {}
        self._done: set[str] = set()
        self.seen = {"entities": 0, "not_small": 0, "naics": 0, "no_website": 0}

    def source_items(self, p, c, cell, limit):
        state = state_of(cell["geo"])
        if not state or state in self._done:
            return [], ""
        rot = p.e.signal("registrants")
        out = []
        for _ in range(MAX_PAGES):
            page = self._page.get(state, 0)
            data, _ = rot.call("new_registrants", state, self.params["days"], page)
            self._page[state] = page + 1
            for row in data["rows"]:
                item = self.item(row)
                if item and not p.known(c, item.key):
                    out.append(item)
            if not data["rows"] or (page + 1) * PAGE_SIZE >= data["total"]:
                self._done.add(state)
                break
            if len(out) >= limit:
                break
        return out, "sam_entity"

    def item(self, row: dict) -> SourceItem | None:
        self.seen["entities"] += 1
        if not row.get("small"):
            self.seen["not_small"] += 1
            return None
        pref = self.params.get("naics_prefix") or ""
        if pref and not str(row.get("primary_naics") or "").startswith(pref):
            self.seen["naics"] += 1
            return None
        url = (row.get("url") or "").strip()
        if url and "://" not in url:
            url = "https://" + url
        domain = registrable_domain(url)
        try:
            reg = date.fromisoformat(row.get("registered") or "")
        except ValueError:
            return None
        name = tidy_name(row.get("dba") or row.get("name") or "")
        naics = row.get("primary_naics") or ""
        text = f"SAM.gov's public entity records show {name} registered on {long_date(reg)}"
        text += f" under NAICS code {naics}." if naics else "."
        return SourceItem(key=domain or f"sam-{row.get('uei')}", name=name, website=url if domain else "",
                          facts=[{"id": "sam_registered", "pillar": "Presence", "text": text}],
                          data={"uei": row.get("uei"), "registered": reg.isoformat(), "naics": naics,
                                "city": (row.get("city") or "").title(), "state": row.get("state")})

    def qualify_source(self, p, c, item, cell):
        if not item.website:
            self.seen["no_website"] += 1
            p.ledger.save_lead(c["id"], item.key, {
                "domain": item.key, "name": item.name, "segment": cell["segment"], "geo": cell["geo"],
                "cell_id": cell["id"], "discovered_by": "sam_entity", "strategy": self.id, "record": item.data,
                "found_at": utcnow().isoformat(), "status": "no_email",
                "reason": "no website listed in SAM.gov (public data has no email)"})
            return "no_email", None
        cand = Candidate(name=item.name, website=item.website, source="sam_entity", raw={"sam": item})
        why = p.excluded(c, cand)     # e.g. a Facebook page listed as the website: not their own site
        if why:
            p.ledger.save_lead(c["id"], item.key, {"domain": item.key, "name": item.name, "strategy": self.id,
                                                   "segment": cell["segment"], "geo": cell["geo"],
                                                   "found_at": utcnow().isoformat(), "status": "excluded",
                                                   "reason": why})
            return "excluded", None
        return p.qualify(c, cand, cell, "sam_entity", self)

    def facts(self, p, c, cand, read):
        item = (cand.raw or {}).get("sam")
        return list(item.facts) if item else []


STRATEGY = SamNewRegistrant
