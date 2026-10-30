# P2 Execution Plan — Personal Reliability

## Approach

Build the **unified obligation plane** (P2-A) first, because every other P2
feature and all of P3/P4 depend on its identity/provenance/status contract.
Then extend the existing bills / calendar / routines / wellness planes *in
place* (P2-B..E) and rewire Today (P2-F) on top of the obligation plane. Do
not create competing services — extend the ones P1 already built.

Each workstream is a small, self-contained, testable change on its own file
ownership, so parallel agents do not collide. The parent integrates at the
stable interface checkpoints.

## Workstreams (file ownership)

| WS | Owner files | Depends on |
| --- | --- | --- |
| **P2-A** Obligation plane | `migrations/012_unified_obligations.py`, `backend/obligations/{__init__,models,service}.py`, `backend/persistence/models/obligations.py`, `backend/persistence/repositories/obligations_repo.py`, `backend/api/endpoints/obligations.py`, `backend/persistence/repositories/registry.py` (add repo), `backend/api/app.py` (register router) | none |
| **P2-B** Bills | `backend/bills/service.py`, `backend/api/endpoints/bills.py`, `backend/persistence/repositories/bills_repo.py`, `backend/bills/extractor.py` | P2-A |
| **P2-C** Calendar | `backend/calendar/service.py`, `backend/calendar/sync.py`, `backend/api/endpoints/calendar.py` (if present) | P2-A |
| **P2-D** Routines + Low Energy | `backend/routines/service.py`, `backend/routines/low_energy.py`, `backend/api/endpoints/routines.py` (if present) | P2-A |
| **P2-E** Wellness + Relationships | `backend/wellness/service.py`, `backend/wellness/trends.py`, `backend/contacts/service.py`, `backend/communications/relationships_service.py` | P2-A |
| **P2-F** Today 3.0 | `backend/briefing/service.py`, `backend/attention/aggregator.py`, `backend/api/endpoints/attention.py`, `web/src/views/TodayView.tsx` | P2-A..E |
| **P2-QA** | `tests/backend/test_obligations*.py`, `tests/backend/test_p2_e2e.py`, `docs/evidence/` | all |

## Contract-first integration

- Freeze the obligation schema + status transition table + dedup-key grammar in
  `docs/contracts/obligation.md` before P2-B..F dispatch.
- P2-F reads the obligation plane read-only; it must not write obligation
  state (snooze/complete go through the obligation service so they stay
  durable + mirrored).
- P3/P4 design against the frozen contracts only; they do not import P2
  in-progress code.

## Verification

- Unit tests per workstream (SQLite scratch DB, no network).
- One durable e2e test: create candidate → confirm → Today deadline → restart
  reload → snooze/complete stays in sync → stale source flagged.
- Live gates recorded as UNVERIFIED in
  `docs/evidence/p2-live-gates.md` (real Postgres / IMAP / Mac relay /
  iPhone/Watch / OmniRoute).

## Non-goals

- No payments, purchases, or account changes.
- No sending personal messages (that is P3).
- No diagnosis / treatment recommendations.
- No autonomous agents (that is P4).
