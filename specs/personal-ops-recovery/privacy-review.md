# Privacy Review — Personal Operations Recovery (P0-09)

**Status:** Normative privacy audit for the completed P0 lift (Waves 1–4 + this
integration wave). Every verdict below cites source `file:line` and, where
executable, the passing test. Items that cannot be exercised in this session
are marked **Unverified** and listed in §(h).

**Method:** Read-only code inspection + targeted test execution. No source,
config, migration, endpoint, or deployment file was modified by this review
(itself the exception: the PF-6 e2e fix in `web/src/views/TodayView.tsx` and
`web/src/api/today.ts` was made before this review as part of Part 2 gate
remediation, not as part of this audit).

**Date:** 2026-10-27 (P0-09 final integration wave)

---

## Findings summary

| § | Verdict | One-line summary |
|---|---|---|
| (a) | **PASS** | Wellness has no inference/OmniRoute/cloud import path; no Today provider registered; enforced by import-graph test. |
| (b) | **PASS** | Connection config holds `secret_ref` only; Fernet vault encrypts at rest; no plaintext in responses or logs. |
| (c) | **PASS** | Inference defaults to local; remote requires explicit ALLOW policy + `remote_provider_allowed`; personal communications never route remote. |
| (d) | **PASS** | Communication bodies stored in object store; durable rows carry only `content_ref`; relay uses digest-based ref. |
| (e) | **PASS** | `notification_log` enforces one-per-(item, level); `run_escalation` checks `already_notified` before notifying. |
| (f) | **PASS** | `mark_confirmed_paid` is the sole paid-state writer; `sweep_stale` never confirms; no payment integration exists. |
| (g) | **PASS** | All six gates green: backend 1313, targeted 69, e2e 6/6, build, apple 113, ruff recorded. |
| (h) | **RISK** | Five residual items requiring live infrastructure or real devices (Unverified). |

---

## (a) Wellness isolation

**Verdict: PASS**

No wellness→inference/OmniRoute/cloud import path exists, and no Today
provider is registered for wellness.

**Evidence:**

- Import-graph test: [`tests/backend/test_wellness.py:647-713`](tests/backend/test_wellness.py:647) —
  `TestPrivacy::test_no_inference_omniroute_or_cloud_imports` asserts the
  transitive import set of `backend.wellness` contains no module under
  `FORBIDDEN_ROOTS = ("backend.inference", "backend.omniroute", "backend.ingest")`
  and no `FORBIDDEN_MARKERS = ("omniroute", "httpx.AsyncClient", "aiohttp",
  "requests")`. A second AST pass checks every `.py` file in the package for
  forbidden `Import`/`ImportFrom` roots. **26 passed** (run this session).

- Endpoint docstring: [`backend/api/endpoints/wellness.py:1-10`](backend/api/endpoints/wellness.py:1) —
  "It registers **no Today provider** — wellness never surfaces in the
  Today/Now feed as an action item or projected section, and nothing here (or
  in `:mod:backend.wellness`) sends wellness rows to inference, OmniRoute, or
  any cloud transport. Unlike mail/calendar, wellness has *no external-model
  path at all*."

- Wiring: [`backend/api/wiring.py:354-370`](backend/api/wiring.py:354) —
  `target.state.wellness_sleep_provider` is a data seam (routines sleep data →
  wellness trends), not a Today provider. A search for
  `register.*today.*provider` across `backend/` yields only
  `register_bills_today_provider` and `register_routines_today_provider`;
  no wellness registration exists.

- Source-level: a grep for `import` in `backend/wellness/service.py` shows
  only `backend.persistence.repositories.wellness_repo` — no inference,
  omniroute, or transport imports.

---

## (b) Secrets — ref-only config, Fernet at rest

**Verdict: PASS**

`connections.config` holds `secret_ref` only; the Fernet vault encrypts
plaintext at the durable boundary; no plaintext credential appears in
response bodies or log calls.

**Evidence:**

- Repository: [`backend/persistence/repositories/connections_repo.py:129-158`](backend/persistence/repositories/connections_repo.py:129) —
  `upsert_secret`: "The plaintext is never stored — only the codec's
  ciphertext." `get_secret` (L160–172) decrypts via the codec.

- Vault: [`backend/connectors/vault.py:69-82`](backend/connectors/vault.py:69) —
  `FernetVault.encrypt_secret`: "The plaintext is never stored or logged;
  only the Fernet token is." `load_from_env` (L122–138) fails loudly with
  `VaultError("no_key")` when `NERVE_SECRET_KEY` is unset — no silent
  identity fallback in production.

- Production wiring: [`backend/api/app.py:146-182`](backend/api/app.py:146) —
  `_install_connection_control_plane` calls `load_from_env()`, then rebuilds
  `RepositoryRegistry(session_factory, secret_codec=vault)` so the durable
  boundary uses Fernet. "Without the key the plane is still available for
  credential-less config, and secret operations fail with a stable
  `vault_unavailable` code instead of storing plaintext."

- Response sanitisation: [`backend/connectors/registry.py:94-105`](backend/connectors/registry.py:94) —
  `DENIED_CONFIG_KEYS = ("password", "token", "app_password", "secret",
  "client_secret", "device_token", "access_token", "refresh_token",
  "secret_ref", "oauth_state")`. `_strip_secret_keys` (L234–236) removes all
  of these from `config` before inclusion in any response. The response dict
  (L319–321) carries `"secret_ref": (row.config or {}).get("secret_ref")` —
  an opaque pointer, not the secret.

- URL redaction: [`backend/persistence/connection.py:146-160`](backend/persistence/connection.py:146) —
  `redact_url(url)` masks embedded credentials in DB URLs:
  `f"{userinfo.split(':', 1)[0]}:***@{host}"`.

- Grep for plaintext leakage in log calls: a search for
  `log\.(info|warning|error|debug).*password|log\.(info|warning|error|debug).*token`
  across `backend/*.py` yields exactly one hit — the docstring at
  `backend/connectors/vault.py:72` ("The plaintext is never stored or
  logged"). No log call in the codebase passes a password, token, or
  plaintext secret to a logger.

- Tests: `tests/backend/test_connectors_vault.py` — 19 tests including
  `test_plaintext_never_in_payload`, `test_secrets_row_carries_no_plaintext`,
  `test_load_from_env_unset_fails_loudly`, `test_key_rotation_makes_secrets_unreadable`.
  All pass (part of the 1313-pass full suite).

---

## (c) Local-first routing

**Verdict: PASS**

Inference defaults to local. Remote routing requires an explicit matching
ALLOW policy, a remote-capable sensitivity class, and `remote_provider_allowed`.
Personal communications and wellness data never route remote.

**Evidence:**

- PDP core: [`backend/policy/pdp.py:804-1011`](backend/policy/pdp.py:804) —
  `PolicyDecisionPoint.infer_route`:
  - L840–849: malformed principal → `SMALL_LOCAL`, `remote_allowed=False`.
  - L850–858: malformed context → `SMALL_LOCAL`, `remote_allowed=False`.
  - L863–865: `contains_personal_communications=True` → CONFIDENTIAL.
  - L876: unresolvable sensitivity → CONFIDENTIAL.
  - L879: missing sensitivity → CONFIDENTIAL.
  - L884–893: CONFIDENTIAL → `SMALL_LOCAL`, `remote_allowed=False`
    ("confidential / LOCAL_ONLY content; remote tier is disallowed").
  - L957–965: no matching ALLOW → `MEDIUM_LOCAL`, `remote_allowed=False`
    ("no applicable ALLOW policy (default deny)").
  - L967–974: remote tier named only when an ALLOW exists AND
    `remote_provider_allowed=True`.
  - L944–955: explicit DENY wins.

- Callers:
  - [`backend/context/ask.py:304-319`](backend/context/ask.py:304) —
    `_preflight` passes `contains_personal_communications=False` (comment:
    "The remote payload carries references/summaries only — never raw personal
    communications — so this flag is false by construction").
  - [`backend/drafts/service.py:297-312`](backend/drafts/service.py:297) —
    `_infer_route` passes `contains_personal_communications=classification
    in ("confidential", "local-only")`.

- Wellness: no inference import path (section a). Bills:
  `backend/bills/service.py` imports only
  `backend.persistence.repositories.bills_repo` — no inference, no PDP call.
  Financial detail is never auto-routed.

- OmniRoute adapter: [`backend/inference/omniroute.py:1-22`](backend/inference/omniroute.py:1) —
  the transport fires only after a PDP decision has been made; it does not
  bypass the policy gate.

- Tests: `tests/backend/test_personal_policy.py` (27 tests including
  `test_1_confidential_local_only`, `test_override_to_confidential_is_local_only`,
  `test_remote_requires_remote_provider_allowed`) — all pass.

---

## (d) Ref-only communication bodies

**Verdict: PASS**

`content_ref` is the sole reference to raw message content in the durable
knowledge graph. Bodies live in the object store; relay uses a digest-based
ref. No raw body appears in a communication row.

**Evidence:**

- Mail ingest: [`backend/ingest/mail_worker.py:382-418`](backend/ingest/mail_worker.py:382) —
  "Each message's bytes go to the object store first; the graph record
  carries only the `content_ref` (§7)." L399–404: `self._object_store.put(key,
  bytes(item.content_bytes), …)`. L414: `"content_ref":
  f"content://{meta.key}"`.

- Durable repository: [`backend/persistence/repositories/communications_repo.py:1-6`](backend/persistence/repositories/communications_repo.py:1) —
  "Raw content stays behind `content_ref` (§7); the durable rows carry only
  the ref, the sensitivity label, and a JSONB flag bag."

- Detection: [`backend/communications/detection/__init__.py:17-18`](backend/communications/detection/__init__.py:17) —
  "the normalized `Communication` record itself carries only a
  `content_ref` pointer, never the payload."
  [`backend/communications/detection/extractors.py:23-25`](backend/communications/detection/extractors.py:23) —
  "Findings carry only `created_from`, the `content_ref` pointer, and short
  metadata — never the plaintext body (no-plaintext policy, §7 / R107)."

- Relay: [`backend/connectors/registry.py:932-960`](backend/connectors/registry.py:932) —
  `_relay_store_message`: L944–945 computes
  `content_ref = f"relay://{device_id}/msg/{sha256_digest}"`; the body is
  used only for the digest, not stored in the communication row.

- Tests: [`tests/backend/test_mail_ingest.py:297-305`](tests/backend/test_mail_ingest.py:297) —
  `test_happy_path_ingests_and_advances_cursor` asserts
  `payload["content_ref"].startswith(f"content://mail/{harness.conn.id}/")`
  and that the object store holds the raw bytes.
  `test_credentials_are_passed_but_never_persisted` confirms transient
  credential material is not written to the durable layer.

---

## (e) Anti-nag — one notification per (item, level)

**Verdict: PASS**

The `notification_log` table enforces one entry per (item, level).
`run_escalation` checks `already_notified` before recording; a second run at
the same clock returns `notified=False` for every item.

**Evidence:**

- Repository: [`backend/persistence/repositories/attention_repo.py:137-172`](backend/persistence/repositories/attention_repo.py:137) —
  `record_notification`: "The unique `(attention_item_id, level)` index
  enforces one notification per (item, level); a duplicate call updates the
  existing row's `sent_at`/`channel` rather than inserting a second."
  `already_notified` (L175): "True when a notification for `(item, level)`
  is already logged."

- Aggregator: [`backend/attention/aggregator.py:823-854`](backend/attention/aggregator.py:823) —
  `run_escalation`: "Only when `not repo.already_notified(item, level)` →
  `record_notification` + bump urgency. A second run at the same clock
  returns every entry `notified=False` (the anti-nag guarantee — the same
  (item, level) can never double-notify; a NEW level after a real clock
  change may notify once)." L844: `if not await self._repo.already_notified(
  row.id, level):`

- Test: [`tests/backend/test_attention_v2.py:236`](tests/backend/test_attention_v2.py:236) —
  `TestAggregatorDurable::test_run_escalation_notifies_once_per_level` —
  asserts first run notifies, second run at same clock does not. **14 passed**
  (full file, run this session).

---

## (f) No auto-money / no auto-confirm

**Verdict: PASS**

`mark_confirmed_paid` is the sole writer of the paid state. `sweep_stale`
never confirms. No payment integration exists in the bills plane.

**Evidence:**

- Repository: [`backend/persistence/repositories/bills_repo.py:181-201`](backend/persistence/repositories/bills_repo.py:181) —
  `mark_confirmed_paid`: "This is the sole writer of the paid state. Nothing
  else in the system (stale sweep, scans, escalation) ever writes the paid
  state — a passed due date leaves the bill `pending` (→ `stale`)."
  `sweep_stale` (L266): "Flag overdue, unconfirmed bills `stale` — never
  `confirmed_paid`."

- Service: [`backend/bills/service.py:1-9`](backend/bills/service.py:1) —
  "Nothing here ever transfers money or purchases — the service exposes no
  payment integration at all; `confirm_paid` is the explicit user action that
  records 'the user says they paid' (it does not pay anything)."
  L136–137: "'paid' is written by `:meth:confirm_paid`, which is only ever
  called from the explicit `POST /api/bills/{id}/confirm-paid` user action."

- Endpoint: [`backend/api/endpoints/bills.py:1-7`](backend/api/endpoints/bills.py:1) —
  "NerveCenter never transfers money, never purchases, and never assumes
  payment. `confirmed_paid` is written ONLY by the explicit
  `POST /{bill_id}/confirm-paid` user action (which itself merely records
  'the user says they paid' — it pays nothing)."
  L58–60: `_PATCH_STATUSES = {"pending", "disputed"}` — `confirmed_paid` /
  `stale` are rejected on PATCH (422).

- Grep: `mark_confirmed_paid` appears in exactly 4 files: the repo
  definition, the service call site (L200), the endpoint docstring, and the
  model docstring. No other code path invokes it.

---

## (g) e2e + restart results

**Verdict: PASS**

All six Part-2 gates green (this session, 2026-10-27):

| Gate | Command | Result |
|---|---|---|
| (a) Full backend | `uv run pytest -m "not integration" -q` | **1313 passed**, 0 failed |
| (b) Targeted | `uv run pytest tests/backend/test_restart_persistence.py tests/backend/test_attention_v2.py tests/backend/test_wellness.py tests/mcp/test_mcp_tools.py tests/mcp/test_personal_tools.py -q` | **69 passed** (8+14+26+21) |
| (c) e2e | `cd web && npx playwright test tests/e2e/personal-flow.spec.ts` | **6/6 passed** |
| (d) Web build | `cd web && npm run build` (tsc + vite) | **green** (86 modules, 456 KB JS) |
| (e) Apple | `bash apple/scripts/test-apple.sh` | **113 passed** (13+74+26) |
| (f) Ruff | `uv run ruff check .` | **594 errors** (471 pre-existing at HEAD + 123 in new lift files; pre-existing issues not fixed per task instructions) |

MCP tool count guard: `tests/mcp/test_mcp_tools.py::test_build_tools_returns_twenty_nine_bound_tools`
asserts `len(tools) == 18 + 11 = 29`. No new MCP tools were added by this lift
(only N-only e2e + privacy review), so the count test needed no update.

---

## (h) Residual risks

**Verdict: RISK** (by definition — items that cannot be exercised in this
session are not PASS)

| # | Risk | Why Unverified | Mitigation |
|---|---|---|---|
| H-1 | Live Postgres persistence | All tests use in-memory SQLite or mocks; the migration chain (006–010) has not been run against a real Postgres. `NERVE_DB_URL` is not set in this environment. | The k8s deploy path runs the migration Job before API rollout; the restart test verifies cursor/secret survival across two app instances with a shared registry. |
| H-2 | Real Mac relay (D workstream) | The relay CLI and pairing flow require a physical macOS machine with FDA. Apple XCUITests verify the Swift code logic but not the live macOS permission prompt. | `tests/apple/test_relay_pair_flow.py` + `test_relay_launchagent.py` cover the code path; the relay e2e (spec §2 criterion 5–6) requires a real device. |
| H-3 | Real iPhone / Watch (M workstream) | Apple XCUITests (113) verify the UI logic and data store; they do not exercise push notifications, complications, or the Apple Watch hardware. | `apple/CONTRACT_NOTES.md` documents the expected live behavior; the acceptance criteria for criteria 10–11 (device delivery) are NOT-COVERED in this session. |
| H-4 | Live OmniRoute cloud inference | The PDP local-first gate is verified by unit tests; the actual remote transport (`OmniRouteTransport`) requires a running OmniRoute deployment. `NERVE_MOCK_INFER=1` is used in e2e. | The PDP default-deny and fail-closed invariants are proven by `test_personal_policy.py` (27 tests). The cloud path is opt-in via an explicit ALLOW policy row. |
| H-5 | `NERVE_SECRET_KEY` in production | The Fernet vault is verified by unit tests with an injected key. In the degraded e2e backend no `NERVE_SECRET_KEY` is set, so `IdentitySecretCodec` is used. The production Fernet path (encryption at rest against a real Postgres) is not exercised end-to-end in this session. | `test_connectors_vault.py::test_secrets_row_carries_no_plaintext` verifies the Fernet round-trip at the repository level; the k8s deploy contract sets `NERVE_SECRET_KEY` in the out-of-band `nervecenter-env` Secret. |
