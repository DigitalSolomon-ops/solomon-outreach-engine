# Sizing providers

`leadgen/providers/sizing.py` - `SIZING = {"census_cbp": CensusCbp}`.

Contract: `establishments(naics, state_fips, county_fips) -> (int | None, units)`.
One API call = 1 unit. `None` means Census returned no row (HTTP 204), usually a
suppressed or empty NAICS x county cell.

## census_cbp - County Business Patterns

Checked 2026-10-22.

| Fact | Value | Source |
|---|---|---|
| Latest vintage | **2023** (`latest_vintage`). The 2024/2025 dataset paths do not exist yet. | Discovery catalog https://api.census.gov/data.json (newest `c_dataset: cbp` entry is 2023, modified 2025-03-06) and https://www.census.gov/data/developers/data-sets/cbp-zbp/cbp-api.html |
| Endpoint | `GET https://api.census.gov/data/2023/cbp?get=ESTAB,NAICS2017_LABEL&for=county:CCC&in=state:SS&NAICS2017=NNNNNN[&key=...]` | cbp-api.html example call (state level), county geography from https://api.census.gov/data/2023/cbp/examples.json |
| Variables | `ESTAB` (int, number of establishments), `NAICS2017` (string, 2017 NAICS code, 2-6 digits, also 31-33 / 44-45 / 48-49 / 00), `NAICS2017_LABEL`. `LFO` and `EMPSZES` are "default displayed" (all legal forms / all size classes). The 2023 vintage still uses NAICS **2017** codes. | https://api.census.gov/data/2023/cbp/variables.json |
| Response | JSON array of arrays: first row is the header, then one row per geography. Values are strings. | Standard Census Data API format (not re-read this session; not shown on the CBP page) |

The adapter reads `ESTAB` by header name, and if a breakdown ever comes back it keeps
the row with `LFO=001` and `EMPSZES=001`.

### API key - IMPORTANT

The premise that Census works keyless at low volume is **no longer true**. The CBP docs
page now says: "All data queries to the Census Data API now require an API key."
Live probe 2026-10-22: a keyless call returns `302` to
`https://api.census.gov/data/missing_key.html` with header `X-DataWebAPI-KeyError: 1`;
a bad key returns the same redirect to `invalid_key.html`.

The adapter still takes `key=None` (and only sends `key` when one is given), but in
practice the `global-census` / `US-Census-Bureau` secret must be wired for it to return
data. `capabilities.yaml` still claims keyless use is possible in spirit; its 2023
endpoint and docs URL are correct.

### Error mapping

| Census response | Raised | Why |
|---|---|---|
| 302 to `*_key.html` / `X-DataWebAPI-KeyError` header (missing or invalid key) | `QuotaExhausted` | Census documents no error JSON for this, only the redirect page. No retry within the period fixes it, so the provider is parked for the period instead of being retried every query. Detected with or without redirect following. |
| 429 / quota words | `QuotaExhausted` | via `base.check_response` |
| 204, or empty 200 body | returns `(None, 1)` | no data for that cell |
| 400 (`error: unknown variable ...`, bad predicate), 5xx, non-JSON | `ProviderError` | via `base.check_response` |
| NAICS not 2-6 digits | `ProviderError` | rejected before any call |

### Geography helpers

`COUNTY_FIPS` - Gaston 37/071, Mecklenburg 37/119, Cabarrus 37/025, Union 37/179,
Iredell 37/097, Lincoln 37/109, Cleveland 37/045, Rowan 37/159 (NC); York 45/091,
Lancaster 45/057 (SC). Source: Census Bureau 2020 national county reference file
https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt
(note that file also lists a Union County **SC**, 45/087 - not the Union County in this table).

`city_to_county("Gastonia, NC") -> ("37", "071")`. It covers Gastonia, Belmont and
Mount Holly (Gaston); Charlotte, Huntersville and Matthews (Mecklenburg); Concord and
Kannapolis (Cabarrus); Mooresville (Iredell); Monroe (Union); Lincolnton (Lincoln);
Shelby (Cleveland); Salisbury (Rowan); Rock Hill and Fort Mill (York SC). Kannapolis
straddles Cabarrus and Rowan and is mapped to Cabarrus; Matthews has a sliver in Union and
is mapped to Mecklenburg. It accepts "City, ST", "City ST", "City, North Carolina", and "Mt.".
An unknown city returns `None`.

### Fixture

The official docs publish example *calls* but no example response body, and the key
requirement blocks a keyless capture. So `tests/fixtures/providers/census_cbp.json` is
built to the documented array-of-arrays shape with the columns this call requests.
**Its ESTAB value (123) is a placeholder, not the real Gaston County 238220 count.**
Replace it with a real keyed capture once the key is wired.
