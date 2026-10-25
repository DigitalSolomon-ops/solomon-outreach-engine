"""#2 weak_site: only sites that are broken, not mobile-friendly, outdated or not on https.

Each reason is a weak_* fact the writer may cite (sitequality.py). A site only a text reader
could load is parked as site_unassessed and re-checked later instead of being excluded.
"""
from .base import Strategy, weak_drop


class WeakSite(Strategy):
    reads_quality = True
    keeps_broken_sites = True     # a broken site is the signal; its email must come from elsewhere

    def drop_site(self, p, c, cand, read):
        return weak_drop(read)

    def facts(self, p, c, cand, read):
        return list(read.quality.get("facts") or [])


STRATEGY = WeakSite
