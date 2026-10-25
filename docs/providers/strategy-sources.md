# Lead-strategy signal sources (leadgen/providers/signals.py)

Each one was checked live on 2026-10-25 before the adapter was written. Fixture tests are in
`tests/test_strategies.py`: every adapter has an out-of-credits case that raises `QuotaExhausted`.
All of them are on free allowances (`paid_monthly_cap_usd: 0`).

## fmcsa_census (strategy fmcsa_new_authority)
- Docs: https://data.transportation.gov/Trucking-and-Motorcoaches/SMS-Input-Motor-Carrier-Census-Information/kjg3-diqy (Socrata SODA 2.1, SoQL).
- Endpoint: `GET https://data.transportation.gov/resource/kjg3-diqy.json`, keyless. An optional `X-App-Token` only raises the throttle. The secret `FMCSA-API-Transportation` in the vault is the FMCSA QCMobile web key, which is a different API and isn't used.
- **Dataset: `kjg3-diqy`, never `az4n-8mr2`.** The Company Census (`az4n-8mr2`) has no email column. `FmcsaCensus.__init__` asserts the id.
- Columns used: `dot_number, legal_name, dba_name, phy_city, phy_state, email_address, add_date, authorized_for_hire`. `add_date` is **text** in `DD-MON-YY` form (`25-SEP-26`), so a date window is an explicit `add_date in (...)` list of day strings, and rows can't be sorted by date (ordered by `dot_number DESC` instead, since higher USDOT numbers are newer). `authorized_for_hire` holds the strings `true`/`false`.
- The dataset is a monthly CSA run. On 2026-10-25 its newest `add_date` was 25-SEP-26 (rows updated 2026-10-13), so the window counts back from the dataset's newest `add_date`, not from today. Counting from today gave 4 rows.
- Ingest gate: before reading rows, `count(*)` and `count(email_address)` for the exact window. Rows with no emails means the ingest is refused. Live NC, 30 days: 336 for-hire carriers, 336 with email. SC: 181/181. All of NC: 50,944 rows, 46,156 with email.
- Quota: Socrata throttles with 429 -> QuotaExhausted. No money is involved.

## sam_entity (strategy sam_new_registrant)
- Docs: https://open.gsa.gov/api/entity-api/
- Endpoint: `GET https://api.sam.gov/entity-information/v3/entities`, `api_key` query parameter. `registrationDate=[MM/DD/YYYY,MM/DD/YYYY]`, `physicalAddressProvinceOrStateCode=NC`, `registrationStatus=A`, `includeSections=entityRegistration,coreData,assertions`, `page`, `size` (max 10).
- **Key:** the secret `SAM-Gov-API-Key` (a SAM.gov account key). The `api-data-gov` key returns **404** on this API.
- Public data has `entityURL` (5 of the first 10 NC registrants) and **no point-of-contact email**. The lead's email comes from that website through the normal domain flow, and an entity with no URL is dropped. Small business = any NAICS entry with `sbaSmallBusiness: "Y"` (from `assertions.goodsAndServices.naicsList`).
- Live NC, 30 days back from 2026-10-25: 199 registrants.
- Quota: rate limits depend on the account role (10/day with no role, 1,000/day with a role). The registry cap assumes 10 a day. 429 -> QuotaExhausted.

## hasdata_indeed (strategy hiring_now)
- Docs: https://docs.hasdata.com/apis/indeed/listing
- Endpoint: `GET https://api.hasdata.com/scrape/indeed/listing?keyword=&location=`, header `x-api-key`. **10 credits** per request, from the same balance as `hasdata_maps` (pool `hasdata_maps`).
- Response: `jobs[]` with `title, company, location, isoDate, sponsored ("True"/"False")`. 49 postings for "auto detailer" around Charlotte on the live test.
- The brief named SerpApi `google_jobs`, but no SerpApi key is in the vault. HasData is connected and on a recurring free allowance.
- Errors: same table as hasdata_maps: 403 = credits used up -> QuotaExhausted, 429 = concurrency -> ProviderError.

## hasdata_reviews (strategy bad_reviews_no_replies)
- Docs: https://docs.hasdata.com/apis/google-maps/reviews
- Endpoint: `GET https://api.hasdata.com/scrape/google-maps/reviews?placeId=&sortBy=newestFirst`, 10 credits, 10 reviews a page, same pool.
- **Owner replies:** a review carries `response` ({date, isoDate, snippet}) when the business replied. The docs don't say this; it was confirmed live: a 5-star shop that replies to everything had `response` on all 10 newest. Without `sortBy=newestFirst` the order is "most relevant" (years old).
- Only `has_reply` and the date leave the adapter. Review text, ratings and reviewer names are never kept.

## tavily_answer (strategy missing_from_ai_answers, first choice)
- Docs: https://docs.tavily.com/documentation/api-reference/endpoint/search
- `POST https://api.tavily.com/search` with `include_answer: "advanced"`, `search_depth: "basic"` (1 credit), Bearer key `global-tavily` (added to the vault 2026-10-25). Same 1,000-credit monthly pool as the `tavily` web-search provider.
- Errors: 432/433 are Tavily's plan and pay-as-you-go limit codes -> QuotaExhausted (base.QUOTA_STATUS). Linkup answers when Tavily is spent.

## linkup_answer (strategy missing_from_ai_answers, fallback)
- Docs: https://docs.linkup.so (same endpoint as the `linkup` search adapter).
- `POST https://api.linkup.so/v1/search` with `outputType: "sourcedAnswer"`, `depth: "standard"`. Returns `{answer, sources[]}`. Same $20/month credit pool as `linkup`.
- The live answer for "best auto detailing businesses in Gastonia, NC" named 5 businesses in bold and listed 5 more from Yelp. Names are pulled from the `**bold**` runs for the evidence record. A map listing counts as named when at least half of its name tokens appear anywhere in the answer. That rule leans toward "named", so "the answer didn't mention you" is only claimed when it's plainly true.
- Errors: 429 with `INSUFFICIENT_FUNDS_CREDITS` / `EXCEED_BUDGET_LIMIT` -> QuotaExhausted. Any other 429 is transient.

## rdap (strategy domain_or_ssl_expiring)
- Docs: https://about.rdap.org/ . `GET https://rdap.org/domain/<domain>` redirects to the registry's RDAP server (Verisign for .com/.net). Keyless.
- Expiry: the `events[]` entry with `eventAction: "expiration"`. 404 = not found (no date, no claim). 429 -> QuotaExhausted.
- Certificate: `signals.cert_expiry(host)` opens a TLS connection to port 443 with verification off (on purpose, so an **expired** certificate can still be read) and parses `notAfter` with `cryptography`. No TLS at all gives `None`, which is no claim.
- Facts state the exact date and never the domain name (the writer's validator allows no domain names in an email).
