"""#5 fmcsa_new_authority: motor carriers newly registered with FMCSA, authorized for hire.

Source: Socrata dataset kjg3-diqy on data.transportation.gov (SMS Input - Motor Carrier Census),
never az4n-8mr2, which has no email column. The email is the one the carrier itself declared in
that same row: no enrichment, no people lookup, never guessed. Before any ingest the window's
count(email_address) is read from the source, and a window with rows but no emails is refused.

"In the last N days" is counted back from the dataset's newest add_date (a monthly data run),
not from today. The physical state must be one of the campaign's geos.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..providers import ProviderError
from ..store import utcnow
from ._util import long_date, state_of, tidy_name
from .base import SourceItem, Strategy

EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PAGES = 5


class FmcsaNewAuthority(Strategy):
    passes = ("source",)

    def __init__(self, entry, params):
        super().__init__(entry, params)
        self._rows: dict[str, list] = {}     # per state, fetched this run
        self._offset: dict[str, int] = {}
        self.counts: dict[str, dict] = {}

    def source_items(self, p, c, cell, limit):
        state = state_of(cell["geo"])
        if not state:
            return [], ""
        rot = p.e.signal("carriers")
        if state not in self.counts:
            self.counts[state], _ = rot.call("count", [state], self.params["days"])
        counts = self.counts[state]
        if counts["rows"] and not counts["with_email"]:
            raise ProviderError(f"fmcsa kjg3-diqy: {counts['rows']} new carriers in {state} but "
                                "count(email_address) is 0; refusing the ingest")
        out = []
        for _ in range(PAGES):
            rows = self._rows.setdefault(state, [])
            while rows and len(out) < limit:
                item = self.item(rows.pop(0))
                if item and not p.known(c, item.key):
                    out.append(item)
            if len(out) >= limit:
                break
            off = self._offset.get(state, 0)
            page, _ = rot.call("new_carriers", [state], self.params["days"], max(limit, 50), off)
            self._offset[state] = off + len(page)
            if not page:
                break
            rows.extend(page)
        return out, "fmcsa_census"

    def item(self, row: dict) -> SourceItem | None:
        dot = str(row.get("dot_number") or "").strip()
        if not dot.isdigit():
            return None
        name = tidy_name(row.get("dba_name") or row.get("legal_name") or "")
        try:
            added = datetime.strptime(row.get("add_date", ""), "%d-%b-%y").date()
        except ValueError:
            return None
        fact = {"id": "fmcsa_registered", "pillar": "Presence",
                "text": f"FMCSA's public carrier census lists {name} under USDOT number {dot}, "
                        f"added on {long_date(added)}."}
        return SourceItem(key=f"fmcsa-{dot}", name=name, email=(row.get("email_address") or "").strip().lower(),
                          facts=[fact], data={"usdot": dot, "added": added.isoformat(),
                                              "city": (row.get("phy_city") or "").title(),
                                              "state": row.get("phy_state") or ""})

    def qualify_source(self, p, c, item, cell):
        base = {"domain": item.key, "name": item.name, "website": "", "nosite": True, "segment": cell["segment"],
                "geo": cell["geo"], "cell_id": cell["id"], "discovered_by": "fmcsa_census",
                "found_at": utcnow().isoformat(), "place_id": None, "maps": {}, "strategy": self.id,
                "record": item.data, "facts": item.facts}
        if not EMAIL.match(item.email):
            p.ledger.save_lead(c["id"], item.key, {**base, "status": "no_email"})
            return "no_email", None
        return p.finish_lead(c, self, item.key, base, item.email, f"fmcsa:kjg3-diqy:{item.data['usdot']}")


STRATEGY = FmcsaNewAuthority
