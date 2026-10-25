"""Is this business's website weak? Broken, not mobile-friendly, or clearly outdated.

A campaign with icp.site_filter "weak" keeps only these leads (the preview offer only lands
with someone whose current site is poor). Every reason is something we saw on the page, worded
as an observation, and becomes a fact the writer may cite.

Mobile and outdated checks need the page as a browser got it (raw HTML); from a text reader
nothing is concluded, so the lead is not weak by default.
"""
from __future__ import annotations

import re
from datetime import date

PARKED = re.compile(r"(this domain (?:is|may be) (?:for sale|parked)|domain (?:is )?for sale|buy this domain|"
                    r"parked (?:free|domain)|account (?:has been )?suspended|website (?:is )?(?:coming soon|under construction)|"
                    r"default (?:web )?page|index of /|it works!|future home of)", re.I)
VIEWPORT = re.compile(r"<meta[^>]+name=[\"']?viewport", re.I)
COPYRIGHT = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)
FLASH = re.compile(r"\.swf\b|application/x-shockwave-flash", re.I)
OLD_TAGS = re.compile(r"<(?:font|marquee|blink|center|frameset)\b", re.I)

FACTS = {
    "broken": "When I tried the website it did not load a working page.",
    "parked": "When I visited the website it showed a placeholder or parked page rather than the business.",
    "not_mobile": "The website's pages are not set up to resize for phones, so on a phone they show as a shrunken desktop page.",
    "old_copyright": "The footer on the website says {year}, so it looks like the site has not been updated in a while.",
    "flash": "Part of the website uses Flash, which current browsers no longer show.",
    "old_markup": "The website is built with older page code that phones and current browsers handle poorly.",
    "not_secure": "The website loads without https, so browsers label it \"Not secure\" next to the address.",
}


def assess(pages, *, fetch_failed: bool = False, today: date | None = None, stale_years: int = 3) -> dict:
    """-> {"weak": bool, "reasons": [ids], "facts": [{"id","text","pillar"}]}"""
    today = today or date.today()
    reasons, facts = [], []

    def add(rid, **fmt):
        reasons.append(rid)
        facts.append({"id": f"weak_{rid}", "pillar": "Presence", "text": FACTS[rid].format(**fmt)})

    if fetch_failed or not pages:
        add("broken")
        return {"weak": True, "reasons": reasons, "facts": facts}
    raw = [p for p in pages if p.raw_html and p.html]
    # Only a fetch that reports where it actually landed (DirectFetch follows redirects) can
    # show an http:// final address; others echo the https:// we asked for, so no false positive.
    if raw and str(raw[0].url).lower().startswith("http://"):
        add("not_secure")
    text = "\n".join((p.markdown or "") + "\n" + re.sub(r"<[^>]+>", " ", p.html or "") for p in pages)
    if len(text.split()) < 25 and not raw:
        add("broken")
    elif PARKED.search(text[:3000]) and len(text.split()) < 400:
        add("parked")
    if raw:
        html = "\n".join(p.html for p in raw)
        if not VIEWPORT.search(html):
            add("not_mobile")
        if FLASH.search(html):
            add("flash")
        if len(OLD_TAGS.findall(html)) >= 3:
            add("old_markup")
    years = [int(y) for y in COPYRIGHT.findall(text) if 1995 <= int(y) <= today.year]
    if years and max(years) <= today.year - stale_years:
        add("old_copyright", year=max(years))
    return {"weak": bool(reasons), "reasons": reasons, "facts": facts}
