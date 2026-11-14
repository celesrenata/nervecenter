# P2 gate audit (personal reliability), 2026-11-14

Owner: "next gate?". Read-only: code search plus live row counts. Nothing changed.

## Finding: P2 is built, but nothing real feeds it

| Plane | Code | Live rows | What feeds it today |
|---|---|---|---|
| Obligations (P2-A) | service, repo, API, Today sections | **0** | only manual `POST /api/obligations` / `POST /api/bills` |
| Bills (P2-B) | deterministic extractor with evidence and per-field confidence, candidate table, confirm-paid, briefing | **bills 0, bill_candidates 0** | **nothing**: `extract_bill_candidate` is never called by mail ingest or any endpoint |
| Appointments (P2-C) | calendar service, sync, conflict detection | **calendar_accounts / events 0** | no calendar connected |
| Routines (P2-D) | routines service, weekday/weekend, medication kinds | **0** | manual only |
| Relationships / follow-ups (P2-E) | unanswered (follow-ups), birthdays | **follow-ups 0** | manual only |
| Today 3.0 (P2-F) | sections, source health, Low Energy collapse | n/a | reads the empty planes above; real content comes from attention items (79) |

The task list's open boxes are partly stale (prep/travel buffer and Low Energy Mode 2.0 are the clearest real gaps), but the gate blocker is the data, not features: the user has mail (689) and Messages (2+) connected and nothing else.

## Options

1. **Mail -> bill candidates (recommended).** Run the existing extractor on incoming mail; a candidate appears for the owner to confirm, and only a confirmed bill becomes a bill and an obligation on Today. Guard: mail that NerveShield rates REVIEW or RESTRICTED never produces a candidate (a phishing "invoice" must not look like a bill). No owner action needed; reversible behind a flag.
2. **Connect Google Calendar.** Unlocks appointments, conflicts and prep reminders (P2-C). Needs the owner to re-consent Google with a calendar scope.
3. Build the remaining standalone features (routines presets, medication reminders, wellness export, relationships). Low value while no data reaches them.

## QA deliverables still open

`tests/backend/test_p2_e2e.py`, `docs/evidence/p2-live-gates.md`, honest `implementation-status.yaml`. These follow whichever option is chosen.

## Update 2026-11-14: mail -> bill candidates built (owner "yes", NS-ADR-095)

- **Correction:** this audit's first draft said the bills Today provider was never registered. That was wrong (my search used the wrong name); `register_bills_today_provider()` runs at startup in `backend/api/wiring.py`, so accepted bills do reach Today.
- **Extractor was untested and over-eager:** 127 candidates from 393 emails (96 marketing). A bill-signal gate (`backend/bills/signal.py`) fixes it; it was tuned against real mail using sender domains, rule names and letter/digit skeletons only (no content read).
- **Live, production code path, dry run over 394 stored emails:** PROPOSED 7, SECURITY_NOT_LOW_PRIORITY 12, NOT_EXTRACTABLE 2, NOT_A_BILL 373; nothing written.
- **Known limits:** AWS invoices carry the amount only in the attached PDF (skipped); 2 emails pass the gate but the extractor finds no amount in its own format (NOT_EXTRACTABLE).
- **Tests:** 26 gate tests, 19 proposer/API/worker tests, 4 Playwright tests; backend suite and 109 Playwright tests pass.

## Update 2026-11-14 (later): stored mail backfilled (owner "yes")

`propose_bills_existing --apply`: 7 proposals created (same counts as the dry run). Billers: Chase ($360, due Dec 5), SiriusXM ($11.99 x3, one linked as a duplicate, one without a due date), Xfinity ($10 x2, due Oct 16, one linked as a duplicate; likely already paid), and one $30 with no payee found ("Unknown biller", no due date). `GET /api/bills/candidates` returns 7. Apple Card and UW did not produce proposals: they are the 2 NOT_EXTRACTABLE emails.

## Update 2026-11-14 (later): the two unreadable bills (owner "theyre bills")

- **Cause:** the extractor searched the raw text for its own `$` pattern; one biller's text part is HTML and the other writes the amount in a form it misses, so it gave up although the gate had the amount. Now the extractor gets the gate's cleaned text, and when it still fails the gate's amount is used with the extractor's payee/due-date readers. Both Apple Card emails ($75) are now proposed; 9 proposals in total.
- **Found on the way:** a past-due notice's "was due Nov 8" became Nov 8, 2027 (the date reader assumes future dates). Past-due notices now keep the past date (marked `adjusted: past_due_previous_year`); any due date more than 120 days out is flagged for review. The two Apple Card proposals created before the fix were deleted (untouched, minutes old) and re-proposed with the corrected date.
- Tests: 25 proposer tests (3 new live-case regressions); backend suite 8884 passed.
