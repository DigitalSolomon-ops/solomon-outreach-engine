# How the live system was verified

Every check below was read back from the running system (Cloud Run executions, Firestore documents, Cloud Scheduler, HTTP), not from a document. Business names and identifiers from the live run are omitted.

| Check | Result |
|---|---|
| Unit and end-to-end tests (offline fakes) | pass |
| `health` job: writes its run document and the inventory of secret names present | pass |
| Strategist plan through the dashboard API: schema-valid, numbers computed from Census County Business Patterns counts and a live probe (at most 3 searches, 10 site reads) | pass |
| Three live sample emails for real local businesses: each passed the code validator and the critic, each claim cited an evidence key, cost recorded per call | pass |
| Engine API refuses unauthenticated requests (403) | pass |
| Unsubscribe service health is public; a forged token is rejected (400) | pass |
| Three schedules on America/New_York, each running its own entrypoint | pass |
| Scheduled runs fire unattended | pass |
| Readiness panel reflects the latest health run (mailbox SPF/DKIM/DMARC, landing page) | pass |
| Loopback: private one-lead campaign -> preflight green -> one send -> prospect-style "interested" reply -> AI reply threaded with the booking link and operator BCC -> sequence stopped -> operator notified | pass, 12 of 12 steps |

## Found and fixed while verifying

- An HTTP library logged full request URLs at INFO, and some providers take their key as a query parameter, so one key reached the logs. URL logging is now off and the key was rotated.
- A model that rejects forced tool choice: data-returning calls moved to structured outputs.
- A provider timeout crashed a plan: network errors now count as a failed provider call.
- Cloud Run's front end answers `/healthz` itself, so the health probe moved to `/health`.
- A long-lived API process kept its start-up view of which keys existed: it now adopts each health run's inventory.
- One vendor's API rejected a documented-looking boolean (`"true"`); a live call caught it before any campaign relied on it.
