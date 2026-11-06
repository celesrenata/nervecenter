# Flag-gated ingestion seam that schedules idempotent mail-security jobs (Task 1.8)

Task 1.8 adds an opt-in, default-OFF seam (`NERVE_MAIL_SECURITY_ENABLED`) that schedules exactly one idempotent `PENDING` `mail_security_jobs` row from the two live ingest paths — `MailIngestWorker._process_batch` and `POST /api/communications/ingest` — reusing the Task 1.2 deterministic identity keys and the Task 1.7 repository. It does not consume the job (that is Task 1.9), starts no background scanner, reimplements no fetch, and makes no provider writes. The chosen durability model is the design §4.4 *reconcilable scanner* fallback: the job is scheduled best-effort beside the already-committed communication, and `scan_missing_jobs()` can reconstruct any job lost in the crash window from committed source identity. The author documents this as strictly weaker than a co-committed transactional outbox and explains why co-commit was rejected (the durable write and the P0-D `flush_durable` gate are not a single enlistable transaction, and joining them would touch cursor-adjacent machinery Phase 1 forbids).

Watch for: the honest durability gap in the best-effort scheduling window (confirmed, by design, recoverable via the sweeper); the API path derives its account scope from `communication.source` rather than a connection id (confirmed, scoped and sanitized, but a different scope namespace than the worker — worth noting, not blocking).

**Verdict**: APPROVED

## High-level view

The flag is read exactly the way the ingest plane reads `NERVE_INGEST_ENABLED`: `source.get(VAR, "").strip() not in ("", "0")`, default OFF. With the flag OFF the seam never binds a repository (wiring only binds when the flag is ON) and both call sites short-circuit on `if not mail_security_enabled()` before any DB touch, so worker and API behavior is byte-identical to today. A test proves the no-op outcome even when a repo is bound.

The P0-D commit gate is untouched in effect. Both seam calls sit *after* the existing pipeline call and *before* the `flush_durable`/`set_cursor` gate, and the seam swallows every exception internally (returns `None`, logs a safe code), so a scheduling failure cannot propagate into `_process_batch`, cannot gate the cursor, and never writes a "safe" row. A dedicated exploding-repo test confirms the cursor still advances and no job row is forged.

The attention path is not edited. No `CommunicationPipeline`, detection, or attention source file appears in the commit; the seam only writes to the isolated `mail_security_jobs` table. A flag-on-vs-off test captures the durable `attention_items` mint and asserts the snapshots are identical while the security-job count differs (0 vs 1).

Scheduling is idempotent on the Task 1.2 `outbox_job_key = (account_scope, source_key, analysis_profile_version)`; the repository upserts on that key, so replay/restart resolves to the single existing row. A three-poll replay test and the sweeper's double-run both assert exactly one row.

Privacy holds: the job row and `meta` carry only account scope, opaque Task 1.2 keys, the opaque `content_ref` pointer, and boolean flags; `_safe_job_meta` defensively drops non-boolean flag values. Canary tests assert the secret/body/sender address never reach the row or the failure log line.

<details>
<summary>Issues (3)</summary>

1. **Best-effort durability window** — A crash between the committed communication and the best-effort job insert leaves no job until `scan_missing_jobs()` runs (confirmed, by design §4.4, documented honestly). No sweeper is scheduled in Phase 1; ensure Task 1.9/2.1 wires recovery before this is relied on operationally. Informational — not blocking for Task 1.8.
2. **API vs worker account-scope asymmetry** — The worker scopes jobs as `conn:<connection_id>`; the API path scopes as `src:<sanitized communication.source>` (confirmed). Both are account-scoped and alphabet-sanitized, but they are different namespaces. Confirm this matches the intended R-023 isolation boundary for API-ingested records before Task 1.9 consumes them.
3. **API path has no connection folder/provider** — The API helper passes `channel` as provider and `communication_id` as both `provider_uid` and `rfc_message_id` with `folder=None` (confirmed). The key is still deterministic and account-scoped, but the identity material is coarser than the worker's. Acceptable for scheduling; note it for the consumer.

</details>

<details>
<summary>Details</summary>

### Flag gating mirrors the ingest plane and is a provable no-op when OFF

`mail_security_enabled()` in `ingestion_seam.py` is a line-for-line mirror of `mail_worker.ingest_enabled()`: it reads `NERVE_MAIL_SECURITY_ENABLED` from an injectable mapping (or `os.environ`) and treats `""`/`"0"` as OFF, everything else ON. Default is OFF. The binding in `wiring.py` only calls `set_mail_security_repository(...)` when `mail_security_enabled()` is true at startup, and teardown always unbinds. This gives two independent OFF guarantees: the repo is never bound, and each call site returns early on the flag check before reading the repo. `TestFlagOffIsNoOp` binds the repo explicitly and still asserts zero jobs, one communication, and an unchanged cursor — proving the flag alone, not the absence of a repo, drives the no-op.

### Cursor advancement is structurally isolated from scheduling

```
_process_batch(...)           # existing durable write + run_pipeline + (NEW) seam.schedule()
  -> returns (ingested, dups, seen)
flush = getattr(self._seam, "flush_durable", None)   # P0-D gate, UNCHANGED
durable_failure = await flush()
if durable_failure: return "error"  # no cursor advance, no seen-fold
await self._imports.set_cursor(...)                  # advance only after gate
```

The seam call is inside `_process_batch`, upstream of the gate, and `MailSecurityIngestionSeam.schedule` wraps its body in `try/except Exception` that logs and returns `None` — it cannot raise into `_process_batch`. The gate code (`flush_durable`, `set_cursor`, the `commit_failed` branch) is byte-identical in the diff. `TestSchedulingFailureIsolation` injects a repo whose `upsert_job` raises (with a canary in the message), then asserts `status == "ok"`, `cursor.cursor_value["uid"] == 5`, one communication, and zero job rows — the failure neither blocks the cursor nor mints a forged safe row. This satisfies R-001.3, R-018, I-07 and the T-16/T-17 intent.

### Attention path is neither edited nor perturbed

No pipeline/detection/attention module is in the commit (`git show HEAD --name-only` lists only the two seam files, the new module, `wiring.py`, `registry.py`, `__init__.py`, and the test). The seam writes exclusively to `mail_security_jobs`. `TestAttentionUnaffected` runs the same attention-minting message twice — flag OFF and flag ON — captures the durable `attention_items` rows (id/title/state/importance) and asserts `on_snapshot == off_snapshot` while `off_jobs == 0` and `on_jobs == 1`. This is a real behavioral equivalence assertion, not a structural check, and satisfies T-26 / R-001.4.

### Idempotent scheduling on the Task 1.2 outbox key

`schedule()` derives `logical_source_key(account_scope, provider, provider_uid, rfc_message_id, folder)` then `outbox_job_key(account_scope, source_key, analysis_profile_version)` and calls `repo.upsert_job(..., state=PENDING)`. Because the key is a pure function of already-committed facts, a retry/restart/replay of the same message resolves to one row. `TestFlagOnSchedulesOne` recomputes the expected key independently and asserts the row's `outbox_job_key`/`source_key` match. `TestIdempotentOnReplay` polls the same item three times and asserts exactly one row. `TestScanMissingJobs` runs the sweeper twice and asserts one persisted row. R-018.2 holds.

### No secret, no raw body in the row or logs

The job `meta` is built by `_safe_job_meta`, which admits only `{"requested": True}`, an optional stringified `content_ref` (an opaque object-store pointer, not body), and a `flags` dict filtered to boolean values via `isinstance(v, bool)` — a caller cannot smuggle text through the flags channel. At the worker call site `content_ref` is the stored object key and `flags` is the mail-flag dict; the subject/body/sender never reach the seam. On failure the seam logs `mail_security.schedule_failed` with `account_scope` and `type(exc).__name__` only — never `str(exc)`, which could echo a body fragment. `TestNoCanaryLeak` asserts the canary, the raw sender address, and the secret are absent from the concatenated row fields, and `test_schedule_failure_log_has_no_canary` uses `structlog.testing.capture_logs` with an exception whose text contains the canary to prove the log line carries a safe code only. T-14 holds.

### No fetch reimplementation, no provider writes, scope stops at 1.8

The seam calls only `repo.upsert_job`; there is no mail-transport call, no provider mutation, no outbound request anywhere in the new code. `wiring.py` binds a repository but starts no background task or consumer. `scan_missing_jobs` is a pure callable, explicitly not wired at import. This satisfies T-29 and the task boundary (Task 1.9 consumption is not present).

### Durability honesty (design §4.4)

The module docstring and commit message both state plainly that a crash in the window between the committed communication and the best-effort insert leaves no job until the sweeper runs, and that this is weaker than a co-committed outbox. The author justifies the fallback by pointing to the P0-D gate not being an enlistable transaction, which matches what the worker code shows (`persist_communication_durably` opens its own session; `flush_durable` governs mirrors separately). This is the design-sanctioned fallback with the required failure evidence, not a silently weakened contract.

### Recorded verification

The commit message records: the new suite `12 passed`; the full non-integration suite `5100 passed, 3 warnings` (baseline 5088 + 12 new, 0 failed); a named regression guard of `122 passed` across `test_mail_ingest`, `test_mail_persistence`, `test_restart_persistence`, `test_detection_wiring`, `test_today`, `test_attention_durable`, plus seam/repo tests; `ruff` clean on the changed lines (pre-existing debt in `wiring.py`/`mail_worker.py`/`registry.py` import blocks confirmed present at HEAD via `git stash`); `pyright` 0 errors on `backend/mail_security`. No new schema/migration (reuses Task 1.7 tables + migration 016). Per instruction these were not re-run; the recorded evidence is consistent with the diff (the six required cases map to real assertions above, and the mail-ingest/restart/detection/attention regression suites are named green).

</details>

<details>
<summary>File map</summary>

- `backend/mail_security/ingestion_seam.py` (NEW) — flag reader, `MailSecurityIngestionSeam.schedule` (swallow-on-failure), repo binding getters/setters, `scan_missing_jobs` sweeper, `_safe_job_meta`.
- `backend/ingest/mail_worker.py` — `_schedule_security_job` called after `run_pipeline`, flag+repo guarded; scope `conn:<connection_id>`.
- `backend/api/endpoints/communications.py` — `_schedule_security_job` called after `get_pipeline().ingest`; scope `src:<sanitized source>`.
- `backend/api/wiring.py` — bind repo onto the seam only when flag ON at startup; unbind on teardown.
- `backend/persistence/repositories/registry.py` — lazy `mail_security` property.
- `backend/mail_security/__init__.py` — export new seam symbols.
- `tests/backend/test_mail_security_ingestion_seam.py` (NEW) — 12 tests covering the eight documented cases.

Full diff: `git -C /Users/celes/sources/celesrenata/nervcenter diff dc357a2 -- backend tests`

</details>
