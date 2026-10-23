# Discovery providers (leadgen/providers/discovery_more.py)

Written from each provider's docs, fetched 2026-10-22. Fixtures are in `tests/fixtures/providers/<name>.json`.

## google_places
- Docs: https://developers.google.com/maps/documentation/places/web-service/text-search ; errors: https://google.aip.dev/193 + `google/api/error_reason.proto` (googleapis)
- Endpoint: `POST https://places.googleapis.com/v1/places:searchText`, headers `X-Goog-Api-Key`, `X-Goog-FieldMask` (required: no mask means an error).
- Params: `textQuery = "<query> in <geo>"`, `pageSize = min(limit,20)` (`maxResultCount` is deprecated). Mask: id, displayName, websiteUri, nationalPhoneNumber, formattedAddress, rating, userRatingCount, types, regularOpeningHours.weekdayDescriptions, businessStatus.
- Unit: 1 request = 1 unit. websiteUri/phone/rating/hours put the call on the **Text Search Enterprise** SKU (1,000 free a month).
- Quota: 429 `RESOURCE_EXHAUSTED` (ErrorInfo reason `RATE_LIMIT_EXCEEDED`/`RESOURCE_QUOTA_EXCEEDED`) -> QuotaExhausted. 403 with reason `BILLING_DISABLED` (or "billing" in message) -> BillingSignal. Body: `{"error":{"code","message","status","details":[ErrorInfo]}}`.
- Surprising: past the free cap Google **bills without any error**, so the ledger cap is the only guard. Results max out at 60 across pages. `persist = "place_id_only"`; rating, hours and categories are transient. The docs example response has no `id`, so the fixture-parsed candidates have `place_id=""`.

## outscraper_maps
- Docs: https://app.outscraper.cloud/api-docs (OpenAPI 3.1, v0.4.3, served as `/api-docs-data.json`) + the official SDK `outscraper-python/outscraper/client.py`.
- Endpoint: `GET https://api.outscraper.cloud/google-maps-search`, header `X-API-KEY`.
- Params: `query="<query>, <geo>"`, `limit`, `async=false` (the default is **true**, which returns 202 and a request id), `language=en`, `fields=` (only the ones we read).
- Unit: billed per **place returned**. "If no places were found... it will consume the usage of one place" -> units = max(1, places).
- Quota: there is none. Past the free tier Outscraper **invoices**, it does not refuse. 402 `{"error":true,"errorMessage":"Please close past due invoices or verify your card information is correct."}` -> BillingSignal (none of base.BILLING_WORDS appear in it, so check_response would only call it quota). 401 bad key, 422 bad params -> ProviderError. A 2xx can also carry `error: true`.
- Response: `{"id","status":"Success","data":[[place,...]]}` with one inner list per query; website is `site`, review count is `reviews`.

## hasdata_maps
- Docs: https://docs.hasdata.com/apis/google-maps/search , https://docs.hasdata.com/api-codes , https://docs.hasdata.com/credits-and-concurrency ; response example from https://hasdata.com/apis/google-maps-api
- Endpoint: `GET https://api.hasdata.com/scrape/google-maps/search`, header `x-api-key`. Params: `q="<query> <geo>"`, `hl=en`, `gl=us` (paging uses `start` and needs `ll`; not used).
- Unit: **10 credits per request**, charged only on 200 + `status:"ok"`. So the 1,000-credit free plan is 100 searches.
- Quota: **403 = "You have used up all your API credits"** -> QuotaExhausted. **429 = concurrency** (free plan allows 1 concurrent request) -> ProviderError, not quota. 401 bad key. 400/500 are retryable and not charged.
- Fixture: the docs API page has no response body; the example is the product page's `localResults` sample, with that page's `types` and `workingHours` samples merged into the same item. Results can be `placeResults` (one object) instead of `localResults`.
- Usage endpoint exists: `GET /user/me/usage` with the same key (not wired; HasData is not card-on-file).

## foursquare_places
- Docs: https://docs.foursquare.com/fsq-developers-places/reference/place-search (+ `/errors`, `/authentication`, `/response-fields`, `/rate-limits`; append `.md`).
- Endpoint: `GET https://places-api.foursquare.com/places/search`, `Authorization: Bearer <service key>`, `X-Places-Api-Version: 2025-06-17` (required).
- Params: `query`, `near=<geo>`, `limit<=50`, `fields=fsq_place_id,name,categories,location,tel,website,date_closed`.
- Unit: 1 request. Only **Pro** fields are requested. rating, hours and stats are **Premium** fields, billed from the first call, so they are deliberately not requested and not filled.
- Quota: the new API documents **no** out-of-credits code (only 400/401/403/404/405/409/500, body `{"message": ...}`). 429 -> QuotaExhausted (the legacy v2 table: `quota_exceeded` 429); otherwise base check_response (402/words).
- Fixture: no example body in the docs; it is built from the reference's response schema. Closed places (`date_closed` set) are dropped.

## geoapify_places
- Docs: https://apidocs.geoapify.com/docs/places/ , https://apidocs.geoapify.com/docs/geocoding/forward-geocoding/ , https://apidocs.geoapify.com/docs/place-details/ (field names), https://www.geoapify.com/pricing-details/ , https://www.geoapify.com/pricing/
- Calls: (1) `GET https://api.geoapify.com/v1/geocode/search?text=<geo>&limit=1&format=json&apiKey=` -> `results[0].place_id`; (2) `GET https://api.geoapify.com/v2/places?categories=..&filter=place:<id>&limit<=20&apiKey=`.
- Unit: credits. Geocode = 1, Places = 1 when limit<=20 -> **2 per search**.
- **At least one category is required. There is no free-text search.** Query words map to documented category keys (for example electrician -> `service.electrician`). Unmapped queries use `service,office,commercial,healthcare` plus the documented `name=` filter. Geoapify has no plumber, HVAC or roofer category.
- Quota: none documented. Limits are "soft": they email, then may block. 429 -> QuotaExhausted, 402 -> BillingSignal, the rest via check_response. Error body shape is not documented.
- Fixture: the geocode part is the docs example (trimmed). Places has no example body; it is built from the documented Response Object plus `website`/`contact.phone`/`opening_hours` (documented on Place Details). Websites are sparse (OSM), and places without one are dropped.

## here_discover
- Docs: https://docs.here.com/geocoding-and-search/reference/get_discover (OpenAPI v7.162), tutorials `code-discover-place`, `code-geocode-area`; errors: https://docs.here.com/identity-and-access-management/docs/error-messages , `/troubleshooting-access-denials`; usage: https://docs.here.com/usage/docs/get-started
- Calls: `GET https://geocode.search.hereapi.com/v1/geocode?q=<geo>&limit=1&apiKey=` -> position, then `GET https://discover.search.hereapi.com/v1/discover?q=<query>&at=<lat,lng>&limit<=100&apiKey=`. Discover **requires** `at` or `in=circle|bbox`; a country code alone is rejected.
- Unit: requests (transactions). **2 per search** (geocode + discover) against the 5,000 cap.
- Quota: 429 (`code 429002`, "Request blocked because too many requests made") -> QuotaExhausted. 403 whose cause names the plan or subscription ("Resource plan attachment has expired/is missing", "No resource plan associated") -> BillingSignal; 402 -> BillingSignal; other 403/401/400 -> ProviderError. Body: `{"status","title","code","cause","action","correlationId","requestId"}`.
- `usage()` returns **None**: the Cost Management Usage API (`usage.bam.api.here.com/v2/usage/realms/{realmId}`) accepts only an OAuth 2.0 bearer token (credentials.properties) plus the org realm ID, not an API key.
- Website is `contacts[].www[].value` (sparse); items without one are dropped. HERE returns no ratings.
