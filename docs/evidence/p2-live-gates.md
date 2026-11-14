# P2 (personal reliability) live gates, 2026-11-14

Owner: "next gate?". Status per gate, using the P1 legend:
PASS LIVE = observed in production · PASS SYNTHETIC = tests only · UNVERIFIED = not run · OPEN = not built.

## Gate matrix

| Area | Gate | Status | Evidence |
|---|---|---|---|
| P2-A obligations | durable plane, API, Today sections | PASS SYNTHETIC | unit tests; **0 live rows**: nothing creates obligations except the manual API |
| P2-A | real source feeds obligations | **OPEN** | bill/appointment/routine -> obligation bridging not built |
| P2-B bills | email -> proposal -> Add bill -> Today -> restart -> confirm paid -> handled | **PASS SYNTHETIC** | `tests/backend/test_p2_e2e.py` (real repos + routers + proposer, file-backed SQLite, restart) |
| P2-B | proposals from real mail | **PASS LIVE** | 9 proposals from real billers (Apple Card, Chase, SiriusXM, UW, Xfinity); 12 emails blocked by security; 0 marketing (`p2-gate-audit.md`) |
| P2-B | owner adds a proposal, bill on Today | **UNVERIFIED** | waiting for the owner to add one |
| P2-B | unpaid bill past due -> stale, never "paid" | PASS SYNTHETIC | e2e test; sweep is wall-clock driven in production |
| P2-B | phishing "invoice" never becomes a bill | PASS SYNTHETIC + LIVE | REVIEW mail blocked in tests; 12 blocked live |
| P2-C appointments | real calendar event, prep, conflicts, DST | **UNVERIFIED** | no calendar connected yet; connect flow fixed (NS-ADR-096): Calendar → pick a Gmail account → grant calendar.readonly |
| P2-C | prep tasks / travel buffer | OPEN | not built |
| P2-D routines | presets, medication kinds, briefing | PASS SYNTHETIC | code + tests; 0 live routines |
| P2-D | Low Energy Mode 2.0 (Done / Later / Help Me, tiny steps) | OPEN | today's Low Energy collapse exists; the 2.0 interaction does not |
| P2-D | Wake fallback on device | UNVERIFIED | needs physical device |
| P2-E wellness / relationships | export, deletion, unanswered, birthdays | PASS SYNTHETIC | code + tests; 0 live follow-ups |
| P2-F Today 3.0 | sections, source health, Low Energy collapse | PASS LIVE | serves attention items (79), source health, and now bills |
| P2-F | iPhone/Watch delivery | UNVERIFIED | needs physical devices (same as P1-205) |

## Honest status

- **Durable and live:** bills from email (proposal stage), Today, source health, mail and Messages planes.
- **Durable, no live data:** obligations, routines, wellness, follow-ups, calendar. The code is there; no source feeds it.
- **Not built:** source -> obligation bridging, appointment prep/travel buffer, Low Energy Mode 2.0.
- `docs/implementation-status.yaml` was **not** edited: it has uncommitted changes from someone else.

## What would move P2 forward

1. Owner adds one proposal on the Bills screen (closes the last P2-B live gate).
2. Owner re-consents Google with a calendar scope (unblocks all of P2-C).
3. Build the remaining features (bridging, prep/travel, Low Energy 2.0), worth doing once real data reaches them.
