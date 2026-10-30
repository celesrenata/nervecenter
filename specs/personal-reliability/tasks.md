# P2 Task List

Legend: `[ ]` open · `[x]` done · `[-]` in progress · `[!]` live-verification gate (UNVERIFIED until run on live source)

## P2-A — Unified Obligation Plane

- [x] Freeze obligation contract: identity, dedup-key grammar, status table, evidence shape, origin semantics (`docs/contracts/obligation.md`).
- [x] Migration `migrations/012_unified_obligations.py` (obligations table, dedup_key unique, indexes).
- [x] ORM `backend/persistence/models/obligations.py` (ObligationRow) + register in `models/__init__.py`.
- [x] `backend/persistence/repositories/obligations_repo.py` (upsert_by_dedup_key, merge evidence, list by status/due, snooze/complete/dismiss).
- [x] `backend/obligations/service.py` (create from source record, refresh verification, dedup, attention mirror, Low Energy pick).
- [x] Register `ObligationsRepository` on `RepositoryRegistry`.
- [x] API `backend/api/endpoints/obligations.py` + router registration in `app.py`.
- [x] Unit tests `tests/backend/test_obligations.py`.

## P2-B — Financial Responsibility Manager

- [ ] Recurring obligation recognition in `bills/service.py` (payee+amount drift tolerance + interval match → `recurrence_of`).
- [ ] Missing-payment confirmation reminder at due+48h (needs_confirmation flag; never assumes paid).
- [ ] Price-increase + overdue notices → notice obligations.
- [ ] Bill → obligation bridging (each bill mirrored as an obligation row with dedup_key; payment confirmation syncs both planes).
- [ ] "What do I owe / when / paid / needs attention" briefing endpoint.
- [ ] Tests: synthetic recurring sequence, drift, dup collapse; [!] live-gate: real bill email.

## P2-C — Appointments and Life Scheduling

- [ ] Preparation tasks + travel buffer per appointment; prep reminder at start − prep_lead.
- [ ] Cross-calendar conflict detection.
- [ ] Required documents surface on Today.
- [ ] Post-appointment follow-up obligations.
- [ ] Timezone/DST correctness; stale-source warning (last_sync age vs threshold).
- [ ] Appointment → obligation bridging (appointment + prep as obligations).
- [ ] Tests: prep generation, conflict, DST edge; [!] live-gate: real calendar event.

## P2-D — Routines, Sleep, Low Energy Mode 2.0

- [ ] Weekday/weekend presets in routines schedule.
- [ ] Morning/evening briefing composition.
- [ ] Medication/personal-care reminder kinds.
- [ ] Low Energy Mode 2.0: `low_energy.py` — single next step, tiny-step breakdown, Done/Later/Help Me, urgent deadlines preserved, no guilt/streaks.
- [ ] Routine → obligation bridging.
- [ ] Tests: schedule presets, LEM collapse keeps urgent item; [!] live-gate: real Wake fallback on device.

## P2-E — Wellness + Relationships

- [ ] Wellness appointment export (on request, explicit); data deletion endpoint; opt-in checks.
- [ ] Relationships: unanswered-message detection; birthdays/committed follow-ups; connection reminders; reflective-question helper (local, opt-in).
- [ ] Relationship → follow-up obligation bridging.
- [ ] Tests: export payload, deletion, unanswered list.

## P2-F — Today 3.0

- [ ] Sectioned Today: Needs You / Coming Up / Handled / Waiting On / Source Health from the obligation plane + source freshness.
- [ ] Handled log (durable) of system-organized actions.
- [ ] Source Health: fresh / stale / disconnected per connector (from ingest cursors + last_verified_at).
- [ ] Low Energy collapse mode wired into Today response.
- [ ] Web TodayView sections; Apple Today surface reads same API.
- [ ] Tests: section content, stale vs empty, LEM; [!] live-gates: real Postgres + iPhone/Watch delivery.

## P2-QA + Evidence

- [ ] `tests/backend/test_p2_e2e.py` durable e2e (SQLite): candidate → confirm → Today → restart → snooze sync → stale flag.
- [ ] `docs/evidence/p2-live-gates.md`: gate matrix with UNVERIFIED status per gate.
- [ ] Update `docs/implementation-status.yaml` with honest status (durable vs in-memory vs live-unverified).
