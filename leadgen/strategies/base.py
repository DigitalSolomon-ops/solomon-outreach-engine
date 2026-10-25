"""The one small interface every strategy implements.

The prospector runs a strategy's passes and calls these hooks; it has no strategy-specific
branches of its own.

  find(p, c, cell, limit)         candidates for one ICP cell -> (list[Candidate], provider)
  drop_listing(p, c, cand)        keep (None) or drop (a reason) BEFORE any site is read
  drop_site(p, c, cand, read)     keep (None) or drop (a Drop) after the site read
  facts(p, c, cand, read)         the cited facts this signal adds to the lead

Passes:
  "site"    candidates with a website: own-site read, email published there or from a people
            provider (verified), never guessed
  "nosite"  map listings with no website: email printed on the business's own BBB profile
  "source"  the strategy's own records (FMCSA, SAM.gov): source_items() + qualify_source()
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Drop:
    outcome: str                  # counted in the run stats
    reason: str                   # stored on the lead
    status: str = "excluded"      # or "site_unassessed" (parked for a re-check)
    retry: bool = False


@dataclass
class SiteRead:
    """What the prospector learned from one candidate's site."""
    pages: list = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    site: object = None           # extract.SiteRead or None


@dataclass
class SourceItem:
    """One record from a strategy's own source (an FMCSA carrier, a SAM.gov entity)."""
    key: str                      # lead key: fmcsa-<dot>, sam-<uei>
    name: str
    email: str = ""               # only when the record itself carries one
    website: str = ""
    facts: list = field(default_factory=list)
    data: dict = field(default_factory=dict)   # kept on the lead (public record fields)


class Strategy:
    passes: tuple[str, ...] = ("site",)
    reads_quality = False         # assess the site for weak_* reasons (sitequality.py)
    keeps_broken_sites = False    # a site that won't load still counts (email must come from elsewhere)
    nosite_depth = 0              # map results to read for no-website listings (0 = the caller's limit)

    def __init__(self, entry: dict, params: dict):
        self.entry = entry
        self.id = entry["id"]
        self.params = params

    # ------------------------------------------------------------------ find
    def find(self, p, c: dict, cell: dict, limit: int):
        return p.e.discover.call("search", cell["query"], cell["geo"], limit,
                                 kinds=c["icp"].get("prefer_kinds") or p.settings["discovery"]["prefer_kinds"])

    def sample_find(self, p, c: dict, cell: dict, limit: int):
        """Preflight samples: a smaller search that prefers maps and falls back to web."""
        return p.e.discover.call("search", cell["query"], cell["geo"], limit,
                                 kinds=c["icp"].get("prefer_kinds") or ["maps", "web"])

    # ------------------------------------------------------------------ keep or drop
    def drop_listing(self, p, c: dict, cand) -> str | None:
        return None

    def drop_site(self, p, c: dict, cand, read: SiteRead) -> Drop | None:
        return None

    def drop_nosite_listing(self, p, c: dict, cand) -> bool:
        return False

    # ------------------------------------------------------------------ facts
    def facts(self, p, c: dict, cand, read: SiteRead) -> list[dict]:
        return []

    # ------------------------------------------------------------------ own source
    def source_items(self, p, c: dict, cell: dict, limit: int):
        return [], ""

    def qualify_source(self, p, c: dict, item: SourceItem, cell: dict):
        raise NotImplementedError

    # ------------------------------------------------------------------ writer
    writer_rule = ""              # extra line for the writer's system prompt, if the signal needs one


def weak_drop(read: SiteRead) -> Drop | None:
    """The weak-site rule (weak_site, weak_or_no_site)."""
    q = read.quality
    if not q.get("weak") and read.pages and not any(pg.raw_html and pg.html for pg in read.pages):
        # Only a text reader answered: the mobile, old-code and https checks never ran, so
        # "not weak" is unproven. Park it for a re-check instead of excluding it for good.
        return Drop("site_unassessed", "only a text reader could load the site", status="site_unassessed", retry=True)
    if not q.get("weak"):
        return Drop("site_not_weak", "site is not weak")
    return None
