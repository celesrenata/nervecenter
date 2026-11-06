# Task 1.7 Review — Account-scoped durable security repositories + migration 016

Verdict: APPROVED

## Scope reviewed
Diff `a0cc35a...HEAD` (single commit `dc357a2`) plus working tree. Changed files:
- `backend/persistence/models/mail_security.py` (new)
- `backend/persistence/repositories/mail_security_repo.py` (new)
- `backend/persistence/alembic/versions/016_mail_security.py` (new)
- `tests/backend/test_mail_security_repo.py` (new)
- `tests/backend/test_schema_param_types.py` (edited — test-guard maintenance only)

Working-tree noise (`.pyc`, `web/node_modules`, untracked `.kiro/`, spec zip) is unrelated to the task and out of scope.

## Invariant checks

1. No raw content in any column — PASS. Scanned all four models and the migration. Every table holds only: UUID ids, capped enum-like code `String`s (`state`, `disposition`, `severity`, `category`, `status`, `explanation_code`), hashes (`sha256` 64-hex), opaque refs (`raw_content_ref`/`normalized_content_ref`/`evidence_ref`, all capped `String`), bounded version tokens, and `JSONB` metadata (`meta`, `missing_parts`, `confidence_value`, `assessment`). The `assessment` JSONB is a dump of the `SecurityAssessment` Pydantic model, which is built entirely from `OpaqueRef`/`Code`/bounded-string fields (models.py §348/§483/§537) — no subject/body/address/token field exists to persist. Manifest fields that could be more identifying (`provider_uid`, `folder_or_label`, `source_message_id`) are not persisted as columns at all; the repo persists only opaque `source_id` + refs.

2. Account scope column + index on every table — PASS. All four tables carry a non-null `account_scope: String(128)` with a dedicated `ix_<table>_account_scope` index, mirroring `calendar_events.account_id`. Verified in both models and migration DDL. `test_account_scope_index_present` parametrizes all four.

3. Repository enforces scope on every read/write — PASS. No un-scoped getter exists. `get_job`/`mark_job_state` fetch by id then return `None` when `row.account_scope != account_scope`; `get_job_by_key`/`get_evidence`/`get_assessment`/`get_findings`/`list_*` filter `account_scope` in the `WHERE`. A get-by-id handed a foreign scope returns `None`, never another account's row. Tests `test_cross_account_*` and `test_same_message_two_accounts_isolated` confirm denial even when the caller supplies account A's exact id/source/hash.

4. Idempotent upserts on deterministic Task 1.2 ids — PASS. `upsert_job` keys on `(account_scope, outbox_job_key)`; `upsert_evidence` on `(account_scope, source_id, revision)`; `upsert_assessment` on `(account_scope, assessment_id)`. All are get-or-create (return existing row, no rewrite of identity columns), backed by scoped `unique=True` indexes. `test_idempotent_*` prove single-row replay; assessment replay does not duplicate findings.

5. Minimum table set only — PASS. Exactly `mail_security_jobs`, `mail_evidence`, `mail_security_assessments`, `mail_security_findings`. No intel/dataset/model/action tables.

6. Migration 016 — PASS. `revision="016_mail_security"`, `down_revision="015_inference_providers"` (confirmed 015 is prior head and 016 is the sole revision chaining to it, so single head). Both `upgrade()` and `downgrade()` present; downgrade drops indexes + tables in reverse. No data migration (brand-new tables), so no row loss and read-only compatibility holds. `test_migration_up_then_down` and `test_migration_single_head` exercise this against the real revision.

7. No wiring — PASS. Diff touches only the four new files plus `test_schema_param_types.py`. No edit to `wiring.py`, mail worker, communications models, app startup, ingest/worker/cursor, or any `__init__.py`. The `test_schema_param_types.py` edit registers the four new table names in the exact-metadata guard set and imports the new module — same pattern already used for bills/calendar/obligations; it is test infrastructure, not production wiring.

8. Required test cases present and meaningful — PASS. Migration up/down clean + single head; cross-account denial for job/evidence/assessment/findings using the foreign account's exact id/source/hash; same-message-two-accounts isolation; idempotent single-row upsert for job/assessment/evidence (plus distinct-revision lineage); canary-absence scan over every column of all four tables; metadata + opaque-ref round-trip; account_scope index presence on all four tables.

## Recorded verification evidence (from commit message, not re-run)
- `tests/backend/test_mail_security_repo.py` => 15 passed (migration reversible, single head 016, cross-account denial, idempotent replay = 1 row, canary absent, round-trip, index present).
- Full suite `-m "not integration"` => 5088 passed, 0 failed (baseline 5073 + 15 new; no regression).
- ruff: new files clean; 16 pre-existing errors in `test_schema_param_types.py` unchanged before/after.
- pyright: `backend/mail_security` 0 errors; both new persistence modules 0 errors.

The recorded evidence satisfies the Done criterion (migration up/down, DB tests, cross-account denials) and the diff upholds every invariant above.

## Notes (non-blocking, no action required)
- The canary test asserts absence rather than injecting the canary into a model field, because the contract models have no raw-content field to inject into; absence-scan over all columns is the correct expression of "there is no path for body text to reach the store." Acceptable.
- New models are not registered in `backend/persistence/models/__init__.py`; the hand-written migration does not depend on autogenerate and tests import the module directly. Consistent with "no wiring" and not a defect for this task.

## Blockers
None.
