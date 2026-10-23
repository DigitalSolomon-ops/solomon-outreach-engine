# Web-search and scrape providers (websearch_more.py, scrape_more.py)

Written from each provider's docs as fetched on 2026-10-22. Code: `leadgen/providers/websearch_more.py`
and `leadgen/providers/scrape_more.py`. Fixtures: `tests/fixtures/providers/<name>.json`, each carrying a
`source` URL and a `note` saying which parts are the docs' own examples and which are built from the schema.

Rule used throughout: running out of credits or hitting a plan limit raises **QuotaExhausted**. A payment
signal raises **BillingSignal**. A per-second or concurrency throttle, a target-site failure, or any other
error raises **ProviderError**. Throttles clear in seconds, and QuotaExhausted would park the provider
for the whole period.

## Linkup (`linkup`, discovery/web)
- Docs: https://docs.linkup.so/pages/documentation/endpoints/search/reference.md, …/platform/errors.md, …/platform/pricing.md
- Endpoint: `POST https://api.linkup.so/v1/search`. Auth: `Authorization: Bearer <key>`.
- Params: `q` = "{query} in {geo}", `depth=standard`, `outputType=searchResults`, `maxResults=limit`.
  Results: `results[]` with `type` text|image and `name`, `url`, `content`. Only text results are kept.
- Cost: $0.005 per call (flash, fast and standard searchResults; deep is $0.05). Units = 1 per request. Free $20/month, professional email needed.
- Errors: body `{statusCode, error:{code,message,details}}`.
  - 429 `INSUFFICIENT_FUNDS_CREDITS` or `EXCEED_BUDGET_LIMIT` → Quota. Pricing page: "If your account runs out, the API will respond with a 429".
  - 429 `TOO_MANY_REQUESTS` → ProviderError.
  - Any other 429 → Quota.
  - 402 (x402 USDC pay-per-request) → Billing.
  - 400 `SEARCH_QUERY_NO_RESULT` → empty result list.
  - The code strings come from the official Python SDK, `_raise_linkup_error`.

## Brave Search (`brave_search`, discovery/web, card on file)
- Docs: https://api-dashboard.search.brave.com/api-reference/web/search/get/index.html.md, …/documentation/guides/rate-limiting/index.html.md, …/documentation/resources/help-feedback/index.html.md, https://brave.com/search/api/
- Endpoint: `GET https://api.search.brave.com/res/v1/web/search`. Auth: `X-Subscription-Token`.
- Params: `q`, `count` (max 20). Results: `web.results[]` with `title`, `url` and `description`.
- Cost: $5 per 1,000 requests ($0.005 each). Units = 1 per request. $5 of free credit per month. Only successful requests are counted.
- Errors:
  - The reference documents 404, 422 and 429 with body `{type, error:{id,status,detail,meta,code}, time}`, but publishes no code strings. `QUOTA_LIMITED` and `RATE_LIMITED` come from a third-party guide.
  - The adapter reads `X-RateLimit-Remaining: <per-second>, <per-month>`.
  - Monthly remaining 0, or a code containing QUOTA → Quota.
  - `RATE_LIMITED` with monthly remaining above 0 → ProviderError.
  - Any other 429 → Quota, the safe default on a card-on-file plan.
  - 402, or any error body mentioning payment, billing, balance, prepaid, charge, overage or usage limit → Billing. Brave documents no payment error shape, and its FAQ says a $0 prepaid balance pauses service.

## Jina Reader (`jina_reader`, scrape, keyless)
- Docs: https://jina.ai/reader/, https://github.com/jina-ai/reader (README), https://r.jina.ai/docs → https://r.jina.ai/openapi.json
- Endpoint: `GET https://r.jina.ai/<url>`.
  - Headers: `Accept: application/json` and `X-With-Links-Summary: true`.
  - `Authorization: Bearer <key>` is sent only when a key is given.
- Response: `{code, status, data:{title, url, content, links, httpStatus, ...}, meta}`.
  - `Page.markdown` = `data.content`, with `raw_html=False` and `html=""`.
  - Links come from the markdown `](url)` targets plus `data.links`.
  - `data.httpStatus >= 400` → ProviderError, because the target failed.
- Cost: keyless is free at 20 RPM. With a key, tokens are charged by output length (10M free tokens per new key). Units = 1 request, the capabilities unit.
- Errors (OpenAPI DTOs, body `{code, status, message, readableMessage}`):
  - 402 `InsufficientBalanceError` (40203) or `TierFeatureConstraintError` (40202) → Quota.
  - 429 `RateLimitTriggeredError` (42903) → ProviderError.
  - 409 `BudgetExceededError`, 422 downstream failure, 451 and 5xx → ProviderError.

## ZenRows (`zenrows`, scrape)
- Docs: https://docs.zenrows.com/api-reference/fetch/fetch-a-page.md, https://docs.zenrows.com/api-error-codes.md, https://docs.zenrows.com/fetch/api-reference.md
- Endpoint: `GET https://api.zenrows.com/v1/?apikey=<key>&url=<url>`. No `js_render` and no `premium_proxy`, so the body is the page as served.
- Cost: 1 credit standard (JS 5, premium 10, both 25). Units come from the `X-Request-Credits` header, default 1. Free plan is 5,000 credits/month.
- Errors: problem+json `{code, status, title, detail, type, instance}`.
  - `AUTH004` (402, usage exceeded), `AUTH014` (key credit cap), `AUTH010`, `AUTH011` and `AUTH012` (plan or subscription limits) → Quota.
  - 429 `AUTH006` and `AUTH008` (concurrency and rate) → ProviderError.
  - Target failures → ProviderError: 404 `RESP002` and `RESP007`, 422 `RESP001`, 400 `RESP008`.

## ScrapingAnt (`scrapingant`, scrape)
- Docs: https://docs.scrapingant.com/request-response-format, …/api-basics, …/headless-browser, …/errors, …/credits-cost
- Endpoint: `GET https://api.scrapingant.com/v2/general?url=<url>&browser=false`.
  - Auth: `x-api-key` header. The docs say the header takes precedence over the query parameter.
  - The default is `browser=true`, which renders JS and costs 10. `browser=false` returns raw HTML for 1 credit.
- Units come from the `Ant-credits-cost` header, default 1. The target's status is in `Ant-page-status-code`; a value of 400 or more → ProviderError.
- Errors: body `{detail}`.
  - 403 "Requests quota limit reached…" → Quota.
  - Other 403s (wrong token, IP whitelist) → ProviderError.
  - 409 (free-plan concurrency, one active request) → ProviderError.
  - 404 (unreachable site), 423 (detection), 422 and 5xx → ProviderError.

## Scrape.do (`scrape_do`, scrape)
- Docs: https://scrape.do/documentation.md, …/documentation/request-costs.md, …/documentation/api-response/status-codes.md, …/documentation/api-response/response-output.md
- Endpoint: `GET https://api.scrape.do/?token=<key>&url=<url>`. Datacenter proxy, no `render` and no `super`, so the body is the raw target HTML. API mode has no header auth.
- Cost: 1 credit (render 5, super 10, both 25). Some domains, such as google.*, get super applied server-side. Units come from the `Scrape.do-Request-Cost` header, default 1. Only successful requests are charged, and the target codes 2xx, 400, 404 and 410 count as success.
- Errors:
  - 401 "no credits or subscription suspended" → Quota. A 401 body saying "incorrect" or "throttled" → ProviderError.
  - 429 (concurrency), 404 (target, charged), 400, 502 and 510 → ProviderError.
  - The docs do not show the error body shape.

## ScraperAPI (`scraperapi`, scrape)
- Docs: https://docs.scraperapi.com/synchronous-apis/using-the-api-endpoint.md, …/responses-and-formats/api-status-codes.md, …/getting-started/quick-start/credits-and-requests-costs.md
- Endpoint: `GET https://api.scraperapi.com/?api_key=<key>&url=<url>`. No `render` and no `premium`, so the body is raw HTML. Timeout is 75 s, because the docs say to allow 70 s for internal retries.
- Cost: 1 credit for normal domains (render 10, premium 10, ecommerce 5, SERP 25, LinkedIn 30, bot-protected domains 10). Units come from the `sa-credit-cost` header, default 1. Charged on 200 and 404.
- Errors:
  - 403 "You have exhausted the API Credits available in this monthly cycle…" → Quota, not Billing. No money is charged unless overages are enabled in the dashboard.
  - 404 (target missing, charged), 429 (concurrency), 400, 401 and 500 → ProviderError.
