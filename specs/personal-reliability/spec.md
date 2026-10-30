# NerveCenter P2 — Personal Reliability Engine

**Status:** Implemented incrementally on `feat/p2-personal-reliability`
**Baseline:** `1a50f42a88464aac5db45ab43fc9daec73ad649f` (`feat/p1-personal-ops-production-activation`)
**Scope:** NerveCenter reliably remembers what matters and helps the user follow through.

## Problem and user outcome

P1 activated ingestion (Connection Center, mail ingest, Apple Messages relay, durable
personal plane, bills/calendar/routines/wellness, Today, mobile). P2 turns that
ingestion into a *reliability* layer: a single canonical obligation model that
survives restarts, deduplicates across email/calendar/Messages, tracks payment and
preparation evidence, and drives a low-interruption Today surface that reduces the
number of responsibilities the user must remember, initiate, and supervise.

**User story:** Wake up, see what matters in one glance. An incoming bill becomes a
reviewable candidate that, once confirmed, shows on Today with the right deadline and
sends a timely reminder — without paying anything. A calendar appointment
auto-generates a preparation reminder. A promised follow-up survives a restart. When
I'm low on energy, Today collapses to one small next step. Snoozing or completing an
obligation stays in sync across devices. Stale connector data is visibly distinct from
an empty inbox.

## Mandatory invariants (carry-forward from P1, non-negotiable)

1. **No autonomous money movement.** NerveCenter never transfers money, pays,
   purchases, or changes an account. `confirmed_paid` is set *only* by an explicit
   user action. A passing due date or a disappearing email never implies payment.
2. **LOCAL_ONLY by default.** Personal content is never sent to a model without an
   explicit, auditable, purpose-scoped route decision. Wellness stays outside
   autonomous agent context.
3. **Every inference cites evidence.** Extracted fields carry a per-field confidence
   map; a low-confidence field goes to `needs_review`, not silently into state.
4. **Durable, restart-safe, idempotent.** All P2 state is in Postgres. Event
   processing is idempotent (dedup keys). Replays are safe.
5. **No synthetic data in production.** Live-verification gates (real Postgres,
   real IMAP, real Mac relay, real iPhone/Watch delivery, production OmniRoute)
   remain UNVERIFIED until run against a live source. Synthetic/in-memory tests
   are *not* production evidence.
6. **Explainable + correctable.** Every recommendation shows why it matters and can
   be snoozed / dismissed / completed / corrected.

## Wave 0 audit matrix (implemented / disconnected / missing)

Evidence: `backend/persistence/models/`, `backend/*/service.py`, `backend/api/app.py`.

| Subsystem | Durable model | Service | API wired | P2 gap |
| --- | --- | --- | --- | --- |
| Bills | `BillRow`/`BillCandidateRow` (`models/bills.py`) | `bills/service.py` (upcoming, monthly calendar, escalation 0–3, attention mirror) | `/api/bills/*` (`app.py`) | recurring-bill detection from repeated candidates; missing-payment confirmation; price-change / overdue notices; cross-source dedup |
| Calendar | calendar plane (`007_calendar`) | `calendar/service.py` (sync, providers) | calendar routes | prep tasks, travel buffers, conflict detection, required-docs, stale-source warning |
| Routines | routines plane (`009_routines`) | `routines/service.py` (schedule eval, missed-window flags, wake fallback alarm, attention mirror) | routine routes | weekday/weekend presets, morning/evening briefings, Low Energy Mode 2.0 |
| Wellness | wellness plane (`010_wellness`) | `wellness/service.py` + `trends.py` | wellness routes | export-for-appointment, data deletion, explicit opt-in checks |
| Attention | `attention_items` durable mirror + in-memory scorer | `attention/service.py`, `aggregator.py` | attention routes | Today 3.0 sections; Low Energy collapse |
| Today/Briefing | reads durable plane | `briefing/service.py` | `/api/today`, `/api/briefing` | Needs You / Coming Up / Handled / Waiting On / Source Health |
| **Unified Obligation** | **MISSING** (concept exists only in bills vocabulary) | **MISSING** | **MISSING** | P2-A canonical Obligation/Commitment/Task/Deadline/Appointment/Bill/Routine/FollowUp/WaitingOn/AttentionItem/EvidenceReference |

**Key architectural finding:** the tree is a *dual plane*. Durable Postgres planes
(bills, calendar, routines, wellness, personal, goals) coexist with in-process
singletons (attention scorer, messages, drafts, briefing) that mirror into durable
`attention_items`. P2-A adds a new durable **obligation plane** that unifies the
actionable subset of those planes behind one identity, provenance, and status
contract — without replacing the existing planes.

## P2-A — Unified Personal Obligation Model

New durable plane `obligations` (migration `012_unified_obligations`):

- Stable identity: `id` (UUIDv4) + `source_account` + `dedup_key`.
- Provenance/evidence: JSONB `evidence` = `[{source_kind, source_id, snippet,
  confidence}]` (same shape as bills).
- Type: `obligation_type` ∈ {obligation, commitment, task, deadline, appointment,
  bill, routine, follow_up, waiting_on}.
- `origin` ∈ {explicit, inferred} — distinguishes user-asserted from
  AI-inferred. AI-inferred never executes on its own.
- `due_at` (tz-aware) or `trigger` JSONB (condition).
- `confidence` + `uncertainty` (JSONB per-field or scalar).
- `status` ∈ {active, scheduled, in_progress, waiting, done, dismissed, snoozed,
  stale}.
- `next_action` (prose) + `suggested_action` (structured).
- `priority` 0–4 + `consequence` (prose).
- `last_verified_at` (tz-aware) — freshness of the underlying source.
- `snooze_until` / `dismissed_at` / `completed_at` semantics.
- `relationships` JSONB = `[{kind, target_id}]`.
- `source_record_ref` JSONB — pointer back to the concrete plane record
  (`{plane: "bills", id: ...}`) so the obligation *references* rather than
  duplicates.

**Deduplication:** `dedup_key` is a normalized
`{type}|{canonical_source_ref}|{canonical_due}` string. Insert is
upsert-on-dedup: if a row with the same `dedup_key` exists, its `evidence` is
merged and `last_verified_at` updated, no second row is created. The same bill
seen in email + calendar + Messages therefore collapses to one obligation with
three evidence references.

**Cross-device sync:** the obligation row is the single source of truth; mobile
surfaces read/write via the same API. Snooze/complete write `snooze_until` /
`completed_at` atomically and emit a durable attention mirror.

## P2-B — Financial Responsibility Manager

Extends `bills/service.py`. Never infers payment from time passing.

- Recurring obligation recognition: a new candidate whose `(payee, amount,
  currency)` matches an existing bill within a drift tolerance and whose due date
  follows the same interval → link as `recurrence_of` (no new bill).
- Missing-payment confirmation: at `due + 48h`, if `status == pending`, create a
  confirmation reminder ("Did you pay this?") — a `needs_confirmation` flag, not a
  payment.
- Price-increase / overdue notices: detect amount drift vs. the recurring
  parent and surface as a notice obligation.
- Duplicate detection: same `dedup_key` as P2-A.
- "What do I owe / when / what have I paid / what needs attention" is answerable
  from `/api/bills/upcoming`, `/api/bills/calendar`, `/api/obligations`.

## P2-C — Appointments and Life Scheduling

Extends `calendar/service.py`.

- Prep tasks + travel buffers: a new appointment can carry `prep` (tasks) and
  `buffer_minutes` (travel/prep) and generates a preparation reminder at
  `start − prep_lead`.
- Conflicts: overlapping events across the user's calendars flagged.
- Required documents: `required_docs` refs surfaced on Today.
- Post-appointment follow-up obligations.
- Timezone/DST correctness via IANA tz per event.
- Stale-source warning: a calendar whose last sync is older than a threshold is
  flagged `stale`, distinguished from "no events".

## P2-D — Sleep, Daily Routines and Executive Support

Extends `routines/service.py` (which already has weekday schedules, missed-window
flags, wake fallback alarm, attention mirror).

- Weekday/weekend presets.
- Morning/evening briefings (composited from Today sections).
- Medication / personal-care reminders (routine `kind`).
- **Low Energy Mode 2.0:** collapse Today to a single recommended next action,
  break large tasks into tiny steps, "Do this with me" guidance, Done/Later/Help
  Me controls, preserve urgent deadlines, no streaks / guilt / punishment.

## P2-E — Personal Wellness and Relationship Support

- Wellness: explicit opt-in; mood/energy/stress/sleep trends (already in
  `trends.py`); **exportable summary for appointments on request**; data
  minimization + easy deletion; no diagnosis, no automated treatment
  recommendations.
- Relationships: important people + contact details, birthdays/committed
  follow-ups, **unanswered messages**, boundary/priority notes, gentle
  connection reminders, reflective questions on request. No covert ranking, no
  relationship decisions made for the user.

## P2-F — Today and Attention Engine 3.0

Today is the central experience with five sections:

1. **Needs You** — 1–3 important actionable decisions (highest-priority
   `active`/`scheduled` obligations).
2. **Coming Up** — bills, appointments, commitments, routines due soon.
3. **Handled** — things the system safely organized/completed (durable
   `handled` log).
4. **Waiting On** — responsibilities awaiting others/services.
5. **Source Health** — what NerveCenter actually knows and whether each source
   is current (fresh vs stale vs disconnected).

Low Energy Mode collapses sections 1–4 to one next step while keeping urgent
deadlines visible. Fewer interruptions, more follow-through — not engagement
maximization.

## P2 Definition of Done

Demonstrate (each with synthetic + live separation):

- An incoming bill becomes a reviewable candidate → user confirms → deadline on
  Today (synthetic provider test, **and** live gate marked UNVERIFIED until run).
- A calendar appointment auto-generates a preparation reminder.
- A promised follow-up survives a restart (durable row + reload).
- Low Energy Mode presents one small next step.
- Snoozing/completing an obligation stays synchronized (durable row read by all
  surfaces).
- Stale connector data is visibly distinct from an empty inbox (Source Health).
- Real personal data stays protected (LOCAL_ONLY route decision; no credential
  leakage).

## Source-control

Branch `feat/p2-personal-reliability` from P1 `1a50f42`. P3/P4 do not depend on
unverified P2 contracts; they design concurrently against the frozen contracts
in `docs/contracts/`.
