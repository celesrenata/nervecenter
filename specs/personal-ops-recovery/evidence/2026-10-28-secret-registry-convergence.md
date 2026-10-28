# Secret Registry Convergence + Redaction + Attention Wire Alignment — Evidence (2026-10-28)

Branch: `feat/p1-personal-ops-production-activation` (from `2f1f472`).
Scope: critical dual-registry divergence fix (Personal-Ops Recovery §3/§6),
credential redaction across planes, and the durable→wire shape alignment.

## What was fixed this session

### 1. Dual-registry convergence (the critical bug)

Root cause: `wire_dependencies` built one `RepositoryRegistry` with an
identity codec, and `_install_connection_control_plane` rebuilt a second
registry with the Fernet vault. The mail worker and calendar sync planes
kept using the identity-codec registry, so rotated OAuth tokens / calendar
credentials were persisted **plaintext** and were unreadable to the other
plane.

- `backend/api/wiring.py` — `wire_dependencies` now builds **one**
  `RepositoryRegistry` bound to the Fernet vault via `_load_secret_codec()`,
  stores the vault on `app.state.secret_vault`, and binds the single
  registry on `app.state.repository_registry`. The control-plane installer
  reuses that registry — no second instance is ever built.
- `backend/persistence/repositories/registry.py` — public `secret_codec`
  accessor so external verification of the bound codec is possible.

### 2. Shared credential redaction

- NEW `backend/connectors/redact.py` — `redact_message(message, *, limit=256)`
  (and `redact_transport_error` compat alias): strips the password half of
  `scheme://user:pass@host` URLs and masks token-shaped substrings
  (JWT / `ya29.` / AWS `AKIA` / Slack `xox*` / long base64url blobs).
  Stdlib-only leaf, safe to import from any plane without a cycle.
- `backend/api/health.py` — every check's `str(exc)` and the per-connection
  `last_error_message` now pass through `redact_message`.
- `backend/ingest/supervisor.py` — worker-build / reconcile-skip /
  reconcile-failed error lines and the per-worker `last_error_message`
  surfaced through the health endpoint are redacted.
- `backend/calendar/sync.py` — secret-unreadable, per-account failure
  (durable `last_error_message`, limit 500 preserved), durable-write and
  loop errors are redacted.
- `backend/ingest/mail_worker.py` — `redact_transport_error` is now a thin
  delegate to the shared helper (public symbol preserved).

### 3. Attention durable-row dump → wire shape

The transition endpoints returned **raw ORM column dumps** (`id`, `title`,
int `importance`, string `urgency`, `state`, …) for durable rows while
list/now returned the in-memory `AttentionItem` wire shape.

- `backend/attention/aggregator.py` — new `row_to_wire(row)`: maps a durable
  `AttentionItemRow` onto the wire contract (float `urgency`/`importance` in
  [0,1], `due` = `snoozed_until` or `deadline_at`, `status` vocabulary,
  `priority` computed with the same explainable formula as
  `AttentionService._score`, clamped to [0,1]).
- `backend/api/endpoints/attention.py` — `_dump_durable` routes
  `attention_items` rows through `row_to_wire`; other tables keep the raw
  column-dump fallback.

## Verification

- `tests/backend/test_secret_codec_wiring.py` (NEW, 6 tests):
  `_load_secret_codec` unit behavior; `wire_dependencies` binds the Fernet
  vault + registry when `NERVE_SECRET_KEY` is set; identity degradation
  without the key; single registry shared across control-plane /
  mail-worker planes with Fernet ciphertext at rest.
  - Note: raw-row probes must select `SecretRow.payload` (a Core
    `Connection.execute(select(Entity))` returns rows, so
    `scalar_one()` on the entity select yields the first column).
- `tests/backend/test_attention_v2.py` — new `TestRowToWire` (4 tests)
  asserting wire keys, float/status vocabulary, state mapping, and
  priority clamping/deadline bonus; `TestDurableEndpoints` dismiss
  assertion updated to the wire contract.
- `tests/backend/test_attention_persistence.py` — durable
  resolve/acknowledge/suppress/snooze wire assertions updated from the old
  raw-row shape (`state`/`resolved_at`/`snoozed_until`) to the aligned
  shape (`status` = RESOLVED/SUPPRESSED/TODAY + `due`).
- Affected: `pytest tests/backend/test_attention_v2.py
  test_attention.py test_attention_persistence.py
  test_secret_codec_wiring.py` → **55 passed**.
- Full backend suite: `pytest tests/backend` → **1293 passed, 3 warnings**
  (pre-existing SAWarning on `evidence.metadata` name and a Pydantic V2.11
  deprecation in `test_detection.py`; no new warnings).

## Limitations

- LSP (basedpyright) in this workspace cannot resolve `.venv` imports —
  "Import could not be resolved" noise on every test import; the venv
  runtime confirms all imports resolve.
- `backend/calendar/sync.py` has two pre-existing basedpyright errors
  (`transport` Any|None, `relayed_events` kwarg) unrelated to this change.
- Live IMAP/calendar round-trips are Unverified (no live credentials in CI);
  the ciphertext-at-rest proof is via the SQLite scratch DB in the new tests.
