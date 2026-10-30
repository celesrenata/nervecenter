# Frozen Contract: Unified Obligation Plane (P2-A)

**Status:** FROZEN for P2 implementation. P3/P4 design against this; any change
requires a new version.

## Identity

- `id`: UUIDv4 string (no DB UUID column).
- `source_account`: string — the connector/account that produced the evidence
  (e.g. `gmail:user@example.com`, `apple-messages:relay`, `manual`).
- `owner_id`: string — the user (personal plane; single-user today, but the
  column exists so shared/family scopes are possible without a migration).
- `dedup_key`: normalized deduplication key (grammar below). Unique index.

## Dedup-key grammar

```
dedup_key = type ":" canonical_source_ref ":" canonical_due
```

- `type` = the `obligation_type` value (lowercase).
- `canonical_source_ref` = a stable, normalized reference to the *originating
  external record* (e.g. the bill's payee+normalized due month, a calendar
  event's UIDs hash, a conversation's canonical id). For `manual` obligations,
  a user-provided stable slug.
- `canonical_due` = the due date normalized to `YYYY-MM-DD` in the obligation's
  timezone, or `-` when there is no due date.

Two records with the same `dedup_key` are the same obligation. Upsert merges
evidence; it never creates a second row.

## Fields (obligations table)

| Column | Type | Notes |
| --- | --- | --- |
| `id` | String | PK, UUIDv4 |
| `created_at` / `updated_at` | DateTime(tz) | server default `now()` |
| `schema_version` | String(16) | default `v0` |
| `obligation_type` | String(32) | obligation, commitment, task, deadline, appointment, bill, routine, follow_up, waiting_on |
| `origin` | String(16) | `explicit` (user-asserted) or `inferred` (AI-derived) |
| `source_account` | String(128) | provenance account |
| `owner_id` | String(64) | user id |
| `dedup_key` | String(255) | unique |
| `title` | String | human label |
| `due_at` | DateTime(tz) | nullable |
| `trigger` | JSONB | nullable — structured trigger condition when no fixed due date |
| `confidence` | Float | 0.0–1.0 |
| `uncertainty` | JSONB | per-field uncertainty map |
| `evidence` | JSONB | `[{source_kind, source_id, snippet, confidence}]` — same shape as bills |
| `status` | String(16) | active, scheduled, in_progress, waiting, done, dismissed, snoozed, stale |
| `next_action` | String | prose next step |
| `suggested_action` | JSONB | structured suggestion (nullable) |
| `priority` | Integer 0–4 | 4 = most urgent |
| `consequence` | String | what happens if ignored (nullable) |
| `last_verified_at` | DateTime(tz) | freshness of the underlying source |
| `snooze_until` | DateTime(tz) | nullable |
| `dismissed_at` | DateTime(tz) | nullable |
| `completed_at` | DateTime(tz) | nullable |
| `relationships` | JSONB | `[{kind, target_id}]` |
| `source_record_ref` | JSONB | `{plane, id}` pointer to the concrete plane record |

## Status transitions

```
active ⇄ scheduled ⇄ in_progress
   │        │            │
   ├────────┴────────────┴──→ waiting   (awaiting another person/service)
   │                          │
   ▼                          ▼
snoozed → (until passed) → active
done (completed_at set)      — terminal
dismissed (dismissed_at set) — terminal (user said "not now / not mine")
stale (source unverified past threshold) — re-enters active on re-verify
```

- `done`/`dismissed` are terminal; only `stale` re-enters `active`.
- `snoozed` is not terminal; when `now ≥ snooze_until` the item returns to
  `active` (or `stale` if the source is unverified).
- Transitions are written by the obligation service only (single writer), and
  emit a durable attention mirror so Today/mobile stay in sync.

## Evidence & confidence semantics

- Every extracted field carries a per-field confidence. A field below the
  threshold (default 0.7) is flagged in `uncertainty` and the obligation is
  `needs_review` (surfaced, not silently executed).
- `origin == inferred` obligations never auto-execute; they require an explicit
  user acceptance to become actionable. `origin == explicit` obligations may be
  surfaced as actionable immediately.
- `last_verified_at` is refreshed on every evidence merge; when
  `now − last_verified_at > threshold` (per source kind) the obligation is
  `stale` and Today marks it as such (distinct from "no items").

## Origin semantics

- **explicit** — the user asserted it (typed it, confirmed the candidate, or it
  came from a first-party source like their own calendar/Health).
- **inferred** — AI-derived from communications/documents. Requires acceptance
  before it is actionable. The system distinguishes these in every UI.

## API surface

```
GET    /api/obligations?status=&type=&due_before=&due_after=&owner=
POST   /api/obligations                 (create explicit)
POST   /api/obligations/accept/{id}     (accept inferred → active)
POST   /api/obligations/{id}/snooze     {until}
POST   /api/obligations/{id}/complete
POST   /api/obligations/{id}/dismiss
POST   /api/obligations/{id}/verify     (refresh last_verified_at from source)
POST   /api/obligations/refresh         (bulk re-verify + dedup pass)
GET    /api/obligations/today           (Today 3.0 sections + Low Energy collapse)
```

## Non-goals / invariants

- The obligation plane *references* plane records; it does not replace
  bills/calendar/routines/wellness. Payment confirmation still happens only in
  the bills plane and is mirrored back.
- No obligation can expand its own permissions. Execution of any action tied to
  an obligation goes through the P3 ActionProposal + PDP boundary.
