# Solomon Outreach Engine

A cold email strategist that runs a B2B outreach campaign end to end, on free API allowances, with the safety rails written in code rather than in prompts.

You give it a plain-language description of who to reach, what you're offering, and your landing page. It reads which API keys you have, proposes a plan, and after you press **Launch** it runs unattended: finds local businesses, reads their own websites, writes every email and follow-up fresh with Claude, sends inside warmup limits, answers replies, and adjusts itself. Every automatic change is reported to the operator by email.

It runs on Google Cloud (Cloud Run, Cloud Scheduler, Firestore, Secret Manager) and is driven from a private operator dashboard over a private API.

> This is a public snapshot of a production system. Infrastructure identifiers, addresses and email addresses are placeholders; the deploy repo is private.

## What it does

```
Cloud Scheduler (America/New_York)
  05:47      health    which keys exist (names only), sending-domain SPF/DKIM/DMARC + blacklist,
                       landing-page checks, test-batch evaluations, monthly free-tier scout
  06:17      prospect  per campaign: reallocate effort across segments -> discover -> read sites ->
                       enrich -> cited brief -> write first touches and due follow-ups -> digest
  */10 8-19  tick      read replies -> classify -> answer or escalate; launch preflight;
                       auto-pause rules; send what is due, exactly once
```

| Piece | What it is |
|---|---|
| `leadgen/strategies/`, `config/strategies.yaml` | The lead signal a campaign targets, as a pluggable registry: one module per strategy behind one interface (find candidates, keep or drop each with a reason, emit cited facts). 11 ship on free data: any site, weak site, weak or no site, new businesses with no website, newly registered FMCSA carriers, new SAM.gov registrants, low-rated businesses that don't reply to reviews, businesses hiring now, no online booking, businesses an AI search answer doesn't name, and domains or SSL certificates about to expire. The writer may cite only the facts the lead's own strategy emits. `python -m leadgen strategy-probe <id>` measures a strategy's find rate live without a campaign or a send. |
| `leadgen/strategist.py` | Turns a description + landing page + key inventory into one schema-validated plan. Every number in it (reachable leads, cost, days to full volume) is computed by code from Census business counts and a small live probe, never by the model. |
| `leadgen/rotation.py`, `capabilities.py` | 30+ provider adapters behind one rotation. Recurring free allowances first, one-time credits as reserve, card-on-file providers stopped at 90% of free, shared accounts checked against the provider's own usage figure. Paid spend is fixed at $0. |
| `leadgen/enrich.py` | Site observations, tech stack, owner name (only when the site states it, with the source sentence kept), and a lead brief in which every claim must cite an evidence key. |
| `leadgen/writer.py` | Haiku writes, a code validator checks, a separate Haiku critic reviews, Sonnet rewrites a failure once, and then a pre-approved template ships or the follow-up is skipped. |
| `leadgen/sender.py` | Exactly-once sending, per-mailbox warmup, a send window spread across runs, threaded follow-ups. |
| `leadgen/replies.py` | Automatic replies with a 4-reply cap per thread and the operator BCC'd on every one. |
| `leadgen/allocate.py` | Thompson sampling over a campaign's own segment x geo cells, with a floor so no cell starves early. |
| `leadgen/api.py` | Private FastAPI service the dashboard talks to. |
| `leadgen/loopback.py` | One command that proves the live send -> reply -> AI reply path on yourself. |

## Design decisions

- **Compiler over generative.** Models author content inside closed boundaries; code decides what ships. The writer only sees offer facts and evidence entries from the lead record. The validator rejects new numbers, extra links, price talk, hype, fake "Re:" subjects, and any personal detail that doesn't trace to the record. Model output that carries data uses structured outputs (JSON schema), and code re-validates it.
- **Exactly once beats at least once.** A send record (`campaign:lead:step`, status `sending`) is created before SMTP and marked `sent` after. A claim with no outcome after 10 minutes becomes `unknown` and is never retried: missing one email is better than sending it twice. A Firestore lease stops overlapping runs.
- **Launch is self-approving, but nothing sends without it.** Launch moves a campaign to `waiting` until automatic preflight passes: landing page live and on-message, unsubscribe service healthy, a mailbox that passes SPF/DKIM/DMARC and the blacklist, five sample drafts that pass validator and critic, budget headroom, and the kill switch off. It then sends a 10-email test batch and becomes active only if the batch comes back clean 48 hours later.
- **Automatic stops.** A campaign pauses at a 3% bounce rate or 5% unsubscribe-plus-negative rate. A mailbox retires itself if its domain is blacklisted. The global kill switch is checked before every individual send.
- **Compliance in code.** CAN-SPAM footer, postal address, advertisement identification, RFC 8058 one-click unsubscribe headers and a signed unsubscribe link are added by code, and a preflight refuses any message missing one. Suppression is global and permanent, keyed by a hash of the address. No guessed email patterns, no consumer addresses, and no harvesting from sites that ask not to be emailed.
- **The budget can't be blasted around.** AI spend is metered from real token usage. 20% of the monthly cap is held for replies; at 100% new first touches stop rather than falling back to templates.
- **Secrets are names in code, values in Secret Manager.** The engine lists secret names to know what's connected and never logs a value. Request URLs aren't logged either, since some providers take keys as query parameters.
- **Adapters from vendor docs, not memory.** Each provider adapter was written from the vendor's current documentation, with a test against the docs' own example response and a test that its out-of-credits response stops the provider for the period.

## Run the tests

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python -m pytest -q tests
```

The suite is offline: providers, Claude, SMTP, IMAP and DNS are faked. It covers plan-schema rejection, validator and critic fallbacks, auto-escalation, exactly-once sends, the lease lock, every reply class, cross-campaign suppression, the Thompson floor, the budget thresholds, the launch state machine, card-on-file safety stops, and each adapter.

## Run it against a real project

Configuration is in `config/`: `settings.yaml` (system defaults, including the postal address used in the compliance footer), `capabilities.yaml` (the provider registry and free allowances) and `models.yaml` (model IDs and prices). Replace the placeholders, create the secrets you have keys for, and deploy with `deploy/deploy.ps1`. `python -m leadgen --help` lists the entrypoints (`health`, `prospect`, `tick`, `serve-api`, `serve`, `loopback`, `strategy-probe`).

See [VERIFICATION.md](VERIFICATION.md) for how the live system was verified.
