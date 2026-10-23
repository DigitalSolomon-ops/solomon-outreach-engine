# Verify + people adapters

Code: `leadgen/providers/verify_more.py` (`VERIFY_MORE`), `leadgen/providers/people.py` (`PEOPLE`).
Fixtures (docs' own examples): `tests/fixtures/providers/<name>.json`. Docs fetched 2026-10-22.

Rule everywhere: only an explicit mailbox-level "deliverable" becomes `ok`; anything ambiguous is `unknown`.
A per-second/minute throttle is a plain `ProviderError` (not "exhausted for the period").

## Verify

### mailboxvalidator
- Docs: https://www.mailboxvalidator.com/api-single-validation
- `GET https://api.mailboxvalidator.com/v2/validation/single?key=&email=&format=json`. Auth: `key` param. 1 credit/call.
- Fixture BUILT from the documented field table (docs have no example JSON).

| Fields | Verdict |
|---|---|
| `is_syntax` false or `is_domain` false | invalid |
| `is_disposable` true | disposable |
| `is_suppressed` true | invalid |
| `is_catchall` true | catch_all |
| `is_verified` true and `status` true | ok |
| `is_verified` false, `is_smtp` true, not server_down, not greylisted | invalid |
| anything else | unknown |

- Quota: HTTP 401, `{"error":{"error_code":10004,"error_message":"Insufficient credits."}}` -> QuotaExhausted. 10001-10003 (key not found/disabled/expired) -> ProviderError.

### zerobounce
- Docs: https://www.zerobounce.net/docs/email-validation-api-quickstart/v2-validate-emails
- `GET https://api.zerobounce.net/v2/validate?api_key=&email=&timeout=30`. 1 credit; unknown results are free (units 0).

| status / sub_status | Verdict |
|---|---|
| valid | ok |
| catch-all | catch_all |
| invalid, spamtrap, abuse | invalid |
| do_not_mail / disposable | disposable |
| do_not_mail / role_based_catch_all | catch_all |
| do_not_mail / role_based | unknown (no deliverability claim) |
| do_not_mail / anything else | invalid |
| unknown, anything new | unknown |

- Quota: body `{"error":"Invalid API Key or your account ran out of credits"}` (HTTP code undocumented) -> QuotaExhausted. One message covers both cases, so a bad key also disables it for the period.

### abstract_email
- Docs: https://docs.abstractapi.com/api/email-validation
- `GET https://emailvalidation.abstractapi.com/v1/?api_key=&email=&auto_correct=false`. 1 credit per request, even for bad input.

| Fields | Verdict |
|---|---|
| `is_valid_format` false | invalid |
| `is_disposable_email` true | disposable |
| `deliverability` UNDELIVERABLE | invalid |
| `is_catchall_email` true | catch_all |
| DELIVERABLE **and** `is_smtp_valid` true | ok |
| otherwise (incl. DELIVERABLE without SMTP pass) | unknown |

- Quota: HTTP 422 "Quota reached" -> QuotaExhausted. 429 (1 req/s on free) -> ProviderError. Error bodies are not documented.

### reoon
- Docs: https://www.reoon.com/articles/api-documentation-of-reoon-email-verifier/
- `GET https://emailverifier.reoon.com/api/v1/verify?email=&key=&mode=power`. Power mode is the only mode that checks the inbox; quick mode marks non-existent addresses valid. Per-mode credit cost is not documented (units 1). Timeout 120 s.

| power status | Verdict |
|---|---|
| safe | ok |
| role_account | ok only if `is_deliverable` and `is_safe_to_send`; catch_all if `is_catch_all`; else unknown |
| catch_all | catch_all |
| invalid, disabled, spamtrap | invalid |
| disposable | disposable |
| inbox_full, unknown, anything new | unknown |
| any quick-mode answer | invalid/disposable/spamtrap mapped; `valid` -> unknown |

- Quota: not documented. Documented error envelope is `{"status":"error","reason":"..."}`; a reason mentioning credit/quota/limit/balance/exceed -> QuotaExhausted (never assumed to renew unattended); other reasons -> ProviderError. 402/429 -> QuotaExhausted via base.

### hunter_verify
- Docs: https://hunter.io/api-documentation/v2#email-verifier
- `GET https://api.hunter.io/v2/email-verifier?email=&api_key=`. **0.5 credit, reported as units 1** (integer units); pool shared with hunter_domain.

| status | Verdict |
|---|---|
| valid | ok |
| invalid | invalid |
| accept_all | catch_all |
| disposable | disposable |
| webmail, unknown | unknown |
| HTTP 202 (still running) / 222 (remote SMTP failure) | unknown, units 1 |
| HTTP 451 claimed_email | invalid, units 0 (owner asked not to be processed) |

- Quota: HTTP 429 "You have reached your usage limit" -> QuotaExhausted. HTTP 403 is Hunter's *rate limit* -> ProviderError. Envelope `{"errors":[{"id","code","details"}]}`.

### getprospect_verify
- Docs: https://getprospect.com/api-docs/email-verifier
- `POST https://api.getprospect.com/v2/email/verify`, body `{"data":{"email":...}}`, header `x-api-key`. 1 credit only for valid/invalid; accept_all/unknown free. Timeout 90 s (docs: up to 75 s).

| status | Verdict |
|---|---|
| valid | ok |
| invalid | invalid |
| accept_all | catch_all |
| unknown, `success:false` (VERIFICATION_ERROR) | unknown |
| `domain_status` disposable | disposable |

- Quota: HTTP 402 `{"success":false,"errors":[{"name":"PAYMENT_REQUIRED","message":"Workspace has reached its credit limit"}]}` (+ `X-Limit-Reached: true`) -> QuotaExhausted. 408/429 -> ProviderError.

## People

All: only emails whose domain equals the queried domain; `verified` only on an explicit valid/verified flag.

### getprospect
- Docs: https://getprospect.com/api-docs/people-search , credits: https://getprospect.com/api-docs/credits-and-limits
- `POST https://api.getprospect.com/v2/people/search`, header `x-api-key`. Filters: `company.domain.include=[domain]` (`match_mode: PRIMARY`), `contact_details.email.status=["valid"]`, `max_person_per_company=3`; `enrich_email: true`. Search free, 1 email credit per revealed email -> units = addresses served (max 3).
- verified = `email.status == "valid"`. Quota: HTTP 402 PAYMENT_REQUIRED -> QuotaExhausted.

### hunter_domain
- Docs: https://hunter.io/api-documentation/v2#domain-search
- `GET https://api.hunter.io/v2/domain-search?domain=&api_key=&limit=10&provenance=found` (free plan: limit+offset <= 10). Units 1 only when >= 1 result (docs: a query is counted only then).
- Drops `provenance: inferred` (pattern-derived). verified = `verification.status == "valid"`; confidence = `confidence`.
- Quota: 429 -> QuotaExhausted; 403 (rate) -> ProviderError.

### tomba
- Docs: https://docs.tomba.io/api/finder#domain-search , costs: https://docs.tomba.io/usage-and-quotas
- `GET https://api.tomba.io/v1/domain-search?domain=&limit=10`, headers `X-Tomba-Key` + `X-Tomba-Secret`. 1 search credit per 10 requested, only when >= 1 address returned.
- verified = `verification.status == "valid"`; `invalid` dropped; confidence = `score`.
- Quota: 402 `{"errors":{"type":"quota_exceeded","message":"Insufficient search quota ...","code":402}}` and 429 `rate_limit` (daily) -> QuotaExhausted.
- Two-value auth: pass the secret as `key="KEY:SECRET"`, JSON `{"key","secret"}`, or `secret=` kwarg.

### apollo
- Docs: https://docs.apollo.io/reference/people-api-search , https://docs.apollo.io/reference/bulk-people-enrichment , https://docs.apollo.io/reference/status-codes
- Header `x-api-key`. (1) `POST https://api.apollo.io/api/v1/mixed_people/api_search?q_organization_domains_list[]=&contact_email_status[]=verified&per_page=3` (0 credits, no emails returned). (2) `POST /api/v1/people/bulk_match` with `{"details":[{"id":...}]}`, `reveal_personal_emails=false` (1 credit per person with data). Units = ceil(`credits_consumed`).
- Kept only when `email_status == "verified"` (so extrapolated/guessed addresses never pass) -> verified True.
- Plan refusal: 403 `API_INACCESSIBLE` / `AUTH.AUTHORIZATION.ENDPOINT_ACCESS_DENIED`, or any 4xx saying "not available on your plan" -> `QuotaExhausted("plan refused: ...")`. 429 (hourly cap) -> QuotaExhausted via base. Out-of-credits shape is undocumented: `status: failed` with a credit error -> QuotaExhausted.
- Both endpoints need a Master key or an endpoint-scoped key; free accounts need a work-email registration.
