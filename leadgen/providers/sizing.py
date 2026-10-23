"""Market-sizing adapters. Each returns (establishments | None, units).

census_cbp: U.S. Census Bureau County Business Patterns, establishment counts by county and
2017 NAICS code. Not a lead source; the strategist uses it to size a segment x geo.

Verified 2026-10-22 against the official discovery catalog (https://api.census.gov/data.json)
and https://api.census.gov/data/2023/cbp/variables.json:
  - newest CBP vintage published: 2023 (2024 and 2025 have no dataset)
  - variables: ESTAB (int, "Number of establishments"), NAICS2017 (string predicate, "2017 NAICS
    code"), NAICS2017_LABEL; LFO and EMPSZES are "default displayed" (all forms / all sizes)
  - geography: for=county:CCC&in=state:SS
  - "All data queries to the Census Data API now require an API key" (cbp-api.html). A keyless
    or bad-key call is answered with 302 -> /data/missing_key.html or /data/invalid_key.html
    and the header X-DataWebAPI-KeyError: 1.
"""
from __future__ import annotations

import re

from .base import ProviderError, QuotaExhausted, check_response, http_client

latest_vintage = 2023
BASE_URL = f"https://api.census.gov/data/{latest_vintage}/cbp"

# 2017 NAICS: 2- to 6-digit codes, the combined sectors (31-33, 44-45, 48-49), and 00 (total).
_NAICS_RE = re.compile(r"^(\d{2,6}|31-33|44-45|48-49)$")

# County FIPS codes from the Census Bureau's 2020 national county reference file,
# https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt
# (STATE|STATEFP|COUNTYFP|COUNTYNS|COUNTYNAME|CLASSFP|FUNCSTAT), read 2026-10-22.
COUNTY_FIPS = {
    "Gaston County, NC": ("37", "071"),
    "Mecklenburg County, NC": ("37", "119"),
    "Cabarrus County, NC": ("37", "025"),
    "Union County, NC": ("37", "179"),
    "Iredell County, NC": ("37", "097"),
    "Lincoln County, NC": ("37", "109"),
    "Cleveland County, NC": ("37", "045"),
    "Rowan County, NC": ("37", "159"),
    "York County, SC": ("45", "091"),
    "Lancaster County, SC": ("45", "057"),
}

# City -> the county that holds it (or most of it). Kannapolis straddles Cabarrus/Rowan and
# Matthews has a sliver in Union; each is mapped to its principal county.
_CITY_COUNTY = {
    ("gastonia", "nc"): "Gaston County, NC",
    ("belmont", "nc"): "Gaston County, NC",
    ("mount holly", "nc"): "Gaston County, NC",
    ("charlotte", "nc"): "Mecklenburg County, NC",
    ("huntersville", "nc"): "Mecklenburg County, NC",
    ("matthews", "nc"): "Mecklenburg County, NC",
    ("concord", "nc"): "Cabarrus County, NC",
    ("kannapolis", "nc"): "Cabarrus County, NC",
    ("mooresville", "nc"): "Iredell County, NC",
    ("monroe", "nc"): "Union County, NC",
    ("lincolnton", "nc"): "Lincoln County, NC",
    ("shelby", "nc"): "Cleveland County, NC",
    ("salisbury", "nc"): "Rowan County, NC",
    ("rock hill", "sc"): "York County, SC",
    ("fort mill", "sc"): "York County, SC",
}
_STATE_NAMES = {"north carolina": "nc", "south carolina": "sc"}


def city_to_county(geo: str) -> tuple[str, str] | None:
    """'Gastonia, NC' -> ('37', '071'). Also accepts 'gastonia nc' or 'Gastonia, North Carolina'.

    Returns None for a city not in the table.
    """
    if not geo:
        return None
    text = re.sub(r"[.,]", " ", geo.lower())
    text = re.sub(r"\s+", " ", text).strip()
    for full, abbr in _STATE_NAMES.items():
        if text.endswith(" " + full):
            text = text[: -len(full)] + abbr
    parts = text.rsplit(" ", 1)
    if len(parts) != 2:
        return None
    city, state = parts
    if city.startswith("mt "):
        city = "mount " + city[3:]
    county = _CITY_COUNTY.get((city, state))
    return COUNTY_FIPS[county] if county else None


def _key_error(resp) -> bool:
    """Census signals a missing/invalid key by redirecting to *_key.html with a KeyError header."""
    for r in [*resp.history, resp]:
        if r.headers.get("X-DataWebAPI-KeyError"):
            return True
        if r.status_code in (301, 302, 303, 307, 308) and "_key.html" in r.headers.get("location", ""):
            return True
    return str(resp.url).endswith(("missing_key.html", "invalid_key.html"))


class CensusCbp:
    def __init__(self, key: str | None = None, **_):
        self.key = key or None

    def establishments(self, naics: str, state_fips: str, county_fips: str):
        naics = str(naics).strip()
        if not _NAICS_RE.match(naics):
            raise ProviderError(f"census_cbp: bad 2017 NAICS code {naics!r} (2-6 digits)")
        params = {"get": "ESTAB,NAICS2017_LABEL", "for": f"county:{county_fips}",
                  "in": f"state:{state_fips}", "NAICS2017": naics}
        if self.key:
            params["key"] = self.key
        with http_client(timeout=30) as c:
            resp = c.get(BASE_URL, params=params)
        if _key_error(resp):
            # No retry fixes this within the period: park the provider instead of hammering it.
            raise QuotaExhausted(f"census_cbp: API key {'rejected' if self.key else 'required'} "
                                 f"(redirected to {resp.headers.get('location') or resp.url})")
        if resp.status_code == 204 or (resp.status_code == 200 and not resp.content.strip()):
            return None, 1
        data = check_response(resp, "census_cbp")
        return _parse(data, naics), 1


def _parse(data, naics: str) -> int | None:
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[0], list):
        return None
    header = data[0]
    if "ESTAB" not in header:
        raise ProviderError(f"census_cbp: no ESTAB column in {header}")
    col = {name: i for i, name in enumerate(header)}
    rows = data[1:]

    def keep(row, name, want):
        return name not in col or str(row[col[name]]) == want

    # LFO / EMPSZES default to the all-forms / all-sizes row; filter in case a breakdown comes back.
    picked = [r for r in rows if keep(r, "NAICS2017", naics) and keep(r, "LFO", "001")
              and keep(r, "EMPSZES", "001")] or rows
    value = picked[0][col["ESTAB"]]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


SIZING = {"census_cbp": CensusCbp}
