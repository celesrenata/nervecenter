# NerveCenter — Gap Analysis (NC-SPEC-2026-010, C0 deliverable)

> **Historical baseline.** This document records the C0 snapshot from 2026-10-10
> (with the explicit reconciliation dated 2026-10-11). It is not a live status
> report. For current capability status, use
> [`current-state.md`](current-state.md) and
> [`docs/implementation-status.yaml`](../implementation-status.yaml). In
> particular, the historical rows describing attention, personal operations,
> web surfaces, and PDP wiring are superseded by those current records.

**Owner:** worker C0-D1b · **Scope:** single-file deliverable; sibling `docs/architecture/current-state.md` referenced by filename only (not read).
**Evidence basis:** in-repo verification at snapshot commit `5f7f791` (2026-10-10). Every `path:line` below was re-verified against this worktree. Where the task's evidence pack could not be confirmed, the item is marked **Unverified** (§4).

**Spec-pack status (reconciled 2026-10-11):** `specs/nervecenter-aws-platform/` — the NC-CAP-001..015 / NC-REQ / NC-API / NC-MS-000..008 / NC-WS-000..017 / T001-T030 pack — **is in-tree in this worktree**. Verified: `spec.md:121` (`### NC-CAP-001`) through `spec.md:227` (`### NC-CAP-015`) enumerate 15 capability headers; `plan.md` has Phases 0–7 (see `plan.md:11`, `:22`, `:38`, `:56`, `:69`, `:87`, `:102`, `:117`); `tasks.md` has 30 task blocks `T001`–`T030` (`tasks.md:48`–`tasks.md:355`). The C0-D1b reconstruction labels used in this document now match the pack and can be re-baselined against it (see §4 items U-1, U-2).

---

## 1. Gap matrix — 15 capabilities

Statuses: **implemented** / **partial** / **absent** / **unknown**. Change types: **new** / **refactor** / **integration**.

| # | Capability (NC-CAP) | Status | Evidence (path:line) | Change type | Owning workstream |
|---|---|---|---|---|---|
| 1 | Resource registry / entity identity (NC-CAP-001, inferred) | **partial** | `backend/knowledge/entity.py:139` (`EntityService`), `:292` (`find_by_alias`), `:353` (`add_alias`); `schemas/base.py:34` (`SCHEMA_VERSION = "v0"`, `content_hash`); aliases exist but **no URI identity / no canonical alias dedup** | new (URI identity layer) | NC-WS-000 |
| 2 | Connector SDK (NC-CAP-002, inferred) | **partial** | ABC `backend/connectors/base.py:524`; `credential_ref` `:591`; checkpoint `last_synced_at` `:595`, `:615`; 4 concrete connectors (`git.py`, `gpu.py`, `host.py`, `kubernetes.py`); **broken** `make_default_registry` `backend/connectors/registry.py:477`, `:497-503` (imports nonexistent `connectors/nix.py`, `connectors/omniroute.py`, `connectors/zoo.py`) with **zero callers** repo-wide | refactor (fix factory) + new (missing connectors) | NC-WS-001 |
| 3 | Event fabric (NC-CAP-003, inferred) | **implemented** | Custom envelope, not a standard bus: `backend/events/publisher.py:124-128` (`publish()` → NATS JetStream), `backend/events/models.py:7-11` (`EventEnvelope` wire contract), subscriber `backend/events/subscriber.py:125-127` (dead-letter list), `:339-343` (undecodable ack), `backend/events/nats_client.py:304-305` (JetStream context). Alembic baseline 001 could **not** be located in this snapshot (no `migrations/001*.py`) — migration line is **Unverified** | integration (only; hardening) | NC-WS-002 |
| 4 | Rules / policy predicates (NC-CAP-004, inferred) | **partial** | `backend/policy/guardrails.py:121` (guard loop, static `GuardConfig`), `backend/knowledge/derivation.py:46` (`DerivationRule` dataclass), `backend/agents/registry.py:220` (agent manifests w/ `loop_protection`). **No** predicate language, no debounce/cooldown anywhere in `backend/` (grep: zero hits) | new (predicate language, debounce) | NC-WS-003 |
| 5 | Durable workflows (NC-CAP-005, inferred) | **partial** | `backend/scheduler/scheduler.py:102`, `backend/tasks/dag.py`, `backend/api/endpoints/runs.py:6` (router `prefix="/api/runs"`, wired at `backend/api/app.py:72`), `backend/agents/registry.py:120`. **No Temporal / no external durable executor** | integration (Temporal or equivalent) | NC-WS-004 |
| 6 | Functions / serverless execution (NC-CAP-006, inferred) | **absent** | No functions runtime, no lambda/worker-function module anywhere in `backend/` (full grep) | new | NC-WS-005 |
| 7 | IAM — authorization PDP (NC-CAP-007, inferred) | **partial (scaffold, no-op)** | `backend/policy/permissions.py:39` (`PermissionEnforcer`), `:93-95` (`_get_subject_level` returns `default_level` unconditionally — enforces nothing). Exported via `backend/policy/__init__.py:14-19` but **no call sites** outside the policy package (grep: zero) | refactor (real PDP) + integration (wiring) | NC-WS-006 |
| 8 | Authentication (NC-CAP-008, inferred) | **partial** | `backend/api/auth.py:1-5` (signed-JWT SSO/OIDC endpoints, spec sso-integration S101); `backend/api/auth_service.py` present; SSO spec `specs/sso-integration/` tracked. Coverage outside `/api/auth/*` not verified | integration (propagate to MCP/CLI surfaces) | NC-WS-007 |
| 9 | Secrets management (NC-CAP-009, inferred) | **partial** | `backend/api/wiring.py:29-36,83-88` (env/secret wiring around connector factories); **no vault / no KMS**; `mcp/auth.py:9-10` in-memory token + user store with hardcoded `admin/password` | new (vault) + refactor (remove hardcoded creds) | NC-WS-008 |
| 10 | Observability (NC-CAP-010, inferred) | **partial** | `backend/observability/__init__.py:27-31` (OTel + Prometheus imports), `:40` (**`" Metrics"` typo in `__all__`** — space breaks export), `:173` (`record_inference`); `pyproject.toml:35-38` (otel/prometheus deps); `backend/api/observability.py:88-92` (`GET /traces/{trace_id}` is a **placeholder** — returns canned data unless `trace_id == "valid-trace-id"`) | refactor (real trace store) + integration (Nerve-Trace-Id, see §2-D4) | NC-WS-009 |
| 11 | Object storage / LocalStack (NC-CAP-011, inferred) | **absent in repo** (adapter present) | `backend/storage/s3.py:5,6,25` (SigV4 httpx adapter targeting AWS S3 / MinIO / LocalStack), `backend/storage/memory.py:1-4` (explicit in-memory only), `tests/backend/test_object_storage.py:21` (MockTransport emulator — no live store). **No LocalStack deployment in repo**; live probe `tools/localstack/probe-results.jsonl` (pro:2026.3.0, SQS/DynamoDB/IAM/CloudFormation ok, SNS disabled, S3/Lambda/EventBridge/StepFunctions Unknown, ENFORCE_IAM no-denial) is **Unverified** — `tools/` not in snapshot | new (deployment) + integration (probe re-run) | NC-WS-010 |
| 12 | Attention engine (NC-CAP-012, inferred) | **absent** | Verified by grep: zero `AttentionItem`/attention implementation hits in `backend/`, `web/src/`, `mcp/`, `schemas/`. Mentioned only in `prompt2.md:195` addendum and `specs/nervcenter-uplift/spec.md:317` (D-H310, "resolved" claim points at persistence models only) | new | NC-WS-011 |
| 13 | Ideas / goals (NC-CAP-013, inferred) | **partial** | `backend/goals/service.py`, `backend/goals/reconciliation.py`; MCP surface `mcp/tools_module.py:173,182,197` (goal tool definitions); no goal lifecycle engine beyond reconciliation | integration (lifecycle + agent loop) | NC-WS-012 |
| 14 | Photos / health (Apple) (NC-CAP-014, inferred) | **absent** | `apple/` contains widget scaffold only (`apple/Widgets/*.swift`, `apple/CONTRACT_NOTES.md`, `apple/Package.swift`); no photo/health capture code | new | NC-WS-013 |
| 15 | Web console (NC-CAP-015, inferred) | **partial** | 11 views verified under `web/src/views/`: `SystemsView.tsx`, `TimelineView.tsx`, `AgentsView.tsx` (all three named in the pack) plus Ask/Decisions/Goals/Incidents/Inference/Login/Overview/TaskDag. Audit trail: `specs/web-quality-audit/`, `audit-gaps.md` (deployment TAG `bee3fa8`, final-round PASS claims — **Unverified** in this snapshot) | refactor (feature completion) | NC-WS-014 |

**Cross-cutting, verified:**
- **Approvals / audit** — implemented: `schemas/approvals.py`, `backend/evidence/service.py` (service present; per-row line refs not individually re-verified, marked Unverified).
- **OmniRoute inference adapter** — `backend/inference/omniroute.py:58-62` (`OmniRouteAdapter`), `backend/inference/policy.py:88-100`; **unwired**: the only reference outside `backend/inference/` is the dead import at `backend/connectors/registry.py:502`.
- **MCP surface** — 18 tools (`mcp/tools_module.py:337` — docstring "Build the 18 §45 Tool instances"), `mcp/core.py:50` (`dispatch_message`, JSON-RPC over stdio + HTTP).
- **Nerve-Trace-Id** — sent by clients (`mcp/api.py:17`, `cli/client.py:23`), **never read server-side** (grep: no header read in `backend/api/`).
- **Kubernetes** — deployments present: `deployments/kubernetes/base/` (api, mcp, web, oauth, nats, postgres, ingress, migrate-job).
- **Internal spec** — `docs/nervcenter-spec.md` (3,566 lines, verified `wc -l`).

Workstreams NC-WS-015..017 have no capability anchor in the available evidence and are presumed cross-cutting (deploy/CI, test/acceptance, spec-baseline) — **Unverified** (U-1).

---

## 2. Fix-first: 6 hard defects

**D1 — Dead connector registry factory.** `backend/connectors/registry.py:497-503`: `make_default_registry()` imports `.nix`, `.omniroute`, `.zoo` — none of which exist in `backend/connectors/` (only `git.py`, `gpu.py`, `host.py`, `kubernetes.py`). Any call raises `ImportError`. **Fix:** ship the missing connectors *or* remove the dead imports and regenerate the factory from the four real types; add an import-time test (`import backend.connectors.registry` + `make_default_registry()` smoke) to CI. **Risk:** low — zero callers repo-wide, so no runtime blast radius; risk is silent false-confidence that a "default" registry exists.

**D2 — Unwired inference layer.** `backend/inference/{omniroute,policy,adapter,telemetry}.py` implement adapter/policy but nothing in `backend/api/` or `mcp/` imports them (grep-verified; sole external reference is the dead import in D1). The system cannot route an inference request. **Fix:** expose inference behind the API (`/api/inference/*` or an `AskView` backend path) using `OmniRouteAdapter` + `InferenceGuardrails` (`backend/policy/guardrails.py`) + `record_inference` (`backend/observability/__init__.py:173`); add request-scoped timeout and fallback. **Risk:** medium — new network egress (OmniRoute), so must inherit PDP checks (D5/D6) and redaction guardrails before exposure.

**D3 — Unauthenticated MCP.** `mcp/core.py:50` (`dispatch_message`) performs no credential check; `mcp/api.py` / `mcp/core.py` / `mcp/__main__.py` contain no `Authorization`/token handling (grep-verified). 18 mutating tools are callable by anyone who reaches the MCP port (k8s deployment exposes it: `deployments/kubernetes/base/mcp-deployment.yaml`). Compounding: `mcp/auth.py:10` ships `admin/password` in source. **Fix:** gate `dispatch_message` on the existing `backend.api.auth_service` (signed JWT from SSO S101); delete the in-memory user store or move it to a disabled dev-only path; add an auth-failure regression test on the wire. **Risk:** high if done naively — breaks existing CLI/MCP clients mid-rollout; mitigate with a grace window + `MCp_AUTH_MODE=off|warn|enforce` flag flipped per-deployment.

**D4 — Nerve-Trace-Id unread server-side.** `mcp/api.py:17` and `cli/client.py:23` define `TRACE_HEADER = "Nerve-Trace-Id"` and every proxied call sends it; no server code reads it (grep-verified). `backend/api/observability.py:88-92` trace endpoint is a hardcoded placeholder. **Fix:** capture the header in a FastAPI middleware, bind it to the OTel context (`backend/observability/__init__.py:29` `trace`), persist spans to a store backing `/traces/{trace_id}`; replace the `"valid-trace-id"` stub. Also fix the `__all__` typo `" Metrics"` at `backend/observability/__init__.py:40`. **Risk:** low — additive middleware; keep header optional so untraced clients keep working.

**D5 — No-op `PermissionEnforcer`.** `backend/policy/permissions.py:93-95`: `_get_subject_level()` ignores the subject/resource and returns `default_level`; enforcement is vacuous. **Fix:** load policy from a typed policy store (DB-backed `schemas` record), evaluate subject→resource→level hierarchy, return explicit deny on missing policy, add unit tests pinning allow/deny matrices. **Risk:** medium — a correct PDP can start denying previously-permitted internal calls; introduce in `warn`-only mode first, ship logs, then enforce.

**D6 — PDP not wired anywhere.** `PermissionEnforcer` is exported (`backend/policy/__init__.py:14-19`) but has no call sites in API, MCP, or agent paths (grep-verified). **Fix:** mount the enforcer as a dependency on mutating API routes and on `mcp.core.dispatch_message` (alongside D3), keyed off the presenting subject from the JWT claims; fail closed when subject cannot be resolved. **Risk:** medium — fail-closed on legacy anonymous paths (e.g., local CLI in dev) will reject; define an explicit `local-dev` subject rather than an open bypass.

---

## 3. Integration-loss / rollback notes (graceful degradation)

- **Object store (C11):** `backend/storage/s3.py` already fails honestly (constructor refuses incomplete config; operations raise `ObjectStoreError`, never silent success — module doc `s3.py:9-14`). LocalStack loss ⇒ connector-dependent features degrade to `unreachable` probes (pattern already in `backend/api/wiring.py:25-36`); in-memory store must stay *explicitly* requested (`storage/memory.py:1-4`). Rollback: point env at MinIO/S3 endpoint — no code path change.
- **Event fabric (C3):** subscriber keeps a bounded dead-letter list and acks undecodable messages (`backend/events/subscriber.py:125-127,339-343`); NATS loss ⇒ publisher must degrade to bounded local spool, not drop; replay path exists (`models.py` `ReplayRequest/ReplayResult`).
- **Connector SDK (C2):** base class already models `disconnected → connecting → connected → degraded/disabled` transitions (`base.py:524+`); a broken connector must degrade its subsystem to stale-snapshot mode, not crash the API process.
- **Inference (C15/D2):** OmniRoute outage ⇒ surface 503 with `Retry-After`, never queue unbounded; guardrail denials must be terminal and logged.
- **IAM PDP (C7/D5/D6):** fail *closed* when policy is unreadable; keep `warn` mode as the one rollback position; never fail open.
- **MCP auth (D3):** rollback position is `MCP_AUTH_MODE=off` (pre-fix parity), logged as security exception; default target state is `enforce`.
- **Registry factory (D1):** removing dead imports is pure deletion — full rollback = re-add the file; adding real connectors is additive and independently reversible.

---

## 4. Unknown / Unverified (explicit)

| ID | Item | Why unverified | Action |
|---|---|---|---|
| U-1 | `specs/nervecenter-aws-platform/` pack (NC-CAP/REQ/API/MS/WS/T ids, semantics) | **RESOLVED 2026-10-11.** Pack in-tree: `spec.md:121`–`spec.md:227` (NC-CAP-001..015), `plan.md:11–117` (Phases 0–7 gates NC-MS-000..008), `tasks.md:48–355` (`T001`–`T030`, 30 blocks). All NC-CAP/NC-WS labels in this document now have in-tree anchors. | Re-baselined: no residual gap |
| U-2 | NC-WS-015..017 mapping | **RESOLVED 2026-10-11.** `spec.md:334` (NC-WS-015 · C15 web console · NC-CAP-014/mocks · Phase 5), `spec.md:335` (NC-WS-016 · C16 iOS · approvals/attention/recovery controls · PhotoKit/HealthKit clients), `spec.md:336` (NC-WS-017 · C17 integration/fault sim · canonical demo NC-MS-008 · Phase 7). | No action |
| U-3 | LocalStack live probe results (`tools/localstack/probe-results.jsonl`: pro:2026.3.0; SQS/DynamoDB/IAM/CloudFormation available; SNS disabled; S3/Lambda/EventBridge/StepFunctions Unknown; ENFORCE_IAM no-denial) | **RESOLVED 2026-10-11.** File in-tree at `tools/localstack/probe-results.jsonl` (19 records). All facts in the item now verifiable per-record (env-endpoint:1, endpoint-health:4, plan-endpoint:5, plan-info-fallback:6, boto3-presence:7, services 8–16, enforce-iam:17, cli-presence:18, cli-subcommands:19). README at `tools/localstack/README.md`. | No action |
| U-4 | LocalStack plan tier + licensing | Never verified — record 5 (`plan-endpoint=unknown/http-404`) leaves plan tier Unknown; `server_edition_claim="pro"` (record 6) is an explicit self-claim, not a plan-tier verification; no license evidence in-repo. | Confirm at procurement; affects durable-workflow (C5) and functions (C6) choices |
| U-5 | Sibling repos / other checkouts | Out of scope for this worktree by task rules | Parent to supply inventory |
| U-6 | Alembic `001:73` (event-stream migration) | **RESOLVED 2026-10-11.** `backend/persistence/alembic/versions/001_initial.py:73` reads `sa.UniqueConstraint("idempotency_key")` exactly as required. | No action |
| U-7 | Approvals/audit row-level evidence (`schemas/approvals.py`, `backend/evidence/service.py` internals) | Files exist and are non-trivial; per-line citations from the pack not individually re-verified | Spot-check during C0 closure |
| U-8 | Web console audit "final PASS" claims (`audit-gaps.md`, TAG `bee3fa8`) | Audit trail present in-repo but deployed-bundle state not reproducible from snapshot | Re-verify against live deploy |
| U-9 | Photos/health beyond `apple/` widget scaffold | No capture code found; may live in sibling repos (U-5) | Inventory pass |

> **Status-authority note (2026-10-11):** `docs/architecture/implementation-status.yaml` (395 lines; `spec_id: NC-SPEC-2026-010`, `artifact: C0`, generated 2026-10-11) is the **authoritative** implementation-status record for NC-SPEC-2026-010. The tracked legacy `docs/implementation-status.yaml` (241 lines; `version: 1`, `updated: 2026-09-29`, `spec_section: 95`) predates that spec ID — it is a §95 workstream tracker (no NC-CAP/NC-WS enumeration) and is kept only as historical record.

---

## 5. Milestones NC-MS-000..008 → gap-closure order

Milestone ids are from the missing spec pack (U-1); the *ordering* below is derivable from the dependency structure and is the recommended sequence:

| MS | Closure focus | Closes | Depends on |
|---|---|---|---|
| NC-MS-000 | Spec baseline + this gap analysis ratified; workstream map re-anchored to `nervecenter-aws-platform` | U-1, U-2 | — |
| NC-MS-001 | Fix-first defects D1–D6 (registry factory, inference wiring, MCP auth, trace-id, PDP logic + wiring) | C2 (part), C7, C8 (part), C10 (part), C15 (part) | MS-000 |
| NC-MS-002 | Resource identity: URI identity + alias canonicalization over the existing entity service | C1 | MS-001 (auth subject needed for writes) |
| NC-MS-003 | Connector completion: real nix/omniroute/zoo connectors or contract change; `make_default_registry` tested | C2 (rest) | MS-001 |
| NC-MS-004 | Object store: LocalStack/MinIO deployment (plan/licensing resolved per U-4), probe re-run, ENFORCE_IAM verification | C11 | MS-002 (entity evidence storage) |
| NC-MS-005 | Rules: predicate language, debounce/cooldown over `DerivationRule` + guardrails | C4 | MS-002, MS-004 (evaluated against evidence) |
| NC-MS-006 | Durable workflows + functions: Temporal (or equivalent) + serverless execution on verified LocalStack/AWS surface | C5, C6 | MS-004, MS-005 (workflows execute rules) |
| NC-MS-007 | Domain surfaces: attention engine, goals lifecycle, photos/health capture | C12, C13, C14 | MS-005 (attention is rule-driven) |
| NC-MS-008 | Web console completion + observability end-to-end (real `/traces/{trace_id}`, Nerve-Trace-Id closed loop) + acceptance audit re-run | C15 (rest), C10 (rest), U-8 | MS-001..007 (console must reflect real state) |

**Ordering invariants:** security (MS-001) precedes any new external surface (MS-004/006); identity (MS-002) precedes anything that persists actor-attributed writes; store (MS-004) precedes workflows (MS-006); rules (MS-005) precede attention (MS-007). MS-000 is a hard gate: do not close any capability row against an unratified workstream map.
