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
