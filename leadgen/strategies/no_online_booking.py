"""#9 no_online_booking: service businesses whose site shows no booking or scheduling tool.

Uses the existing no_booking observation from extract.py, which is only made from raw HTML
(the page as a browser got it, scripts intact), as the README requires for absence claims. A
site only a text reader could load is parked for a re-check, never counted as "no booking".
"""
from __future__ import annotations

from .base import Drop, Strategy


class NoOnlineBooking(Strategy):

    def drop_site(self, p, c, cand, read):
        raw = [pg for pg in read.pages if pg.raw_html and "<script" in (pg.html or "").lower()]
        if not raw:
            return Drop("site_unassessed", "only a text reader could load the site", status="site_unassessed",
                        retry=True)
        if not any(f["id"] == "no_booking" for f in (read.site.facts if read.site else [])):
            return Drop("booking_found", "the site has a booking tool")
        return None


STRATEGY = NoOnlineBooking
