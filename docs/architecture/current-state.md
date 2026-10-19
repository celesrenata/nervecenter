# NerveCenter — Current State

**Spec:** NC-SPEC-2026-010 (AWS-like local cloud, C0 deliverable)
**Date:** 2026-10-10
**Baseline:** [docs/nervcenter-spec.md](docs/nervcenter-spec.md) (3,566 lines per `wc -l`)

This document inventories what exists and works in this checkout, against the
capability list in NC-SPEC-2026-010. Every claim cites `path:line` verified in
this worktree on 2026-10-10. Items that could not be verified are marked
**Unverified**. Components flagged **MUST-NOT-REWRITE** are load-bearing: any
rework must preserve their contracts.

## What Exists and Works

Verdict key: IMPLEMENTED / PARTIAL / ABSENT.

| # | Capability | Verdict |
| --- | --- | --- |
| 1 | Resource registry | PARTIAL |
| 2 | Connector SDK | PARTIAL |
| 3 | Event fabric | IMPLEMENTED (custom envelope, not CloudEvents) |
| 4 | Rules / triggers | PARTIAL |
| 5 | Durable workflows | PARTIAL |
| 6 | Functions / OCI sandbox | ABSENT |
| 7 | IAM PDP | PARTIAL (scaffold; functional no-op — see Hard Defects) |
| 8 | Secrets | PARTIAL |
| 9 | Observability | PARTIAL |
| 10 | LocalStack / object storage | LocalStack ABSENT repo-wide; S3 client + memory backend IMPLEMENTED |
| 11 | Attention queue | ABSENT |
| 12 | Ideas / goals | PARTIAL |
| 13 | Photos / health / Apple connectors | ABSENT (Apple shells exist, no HealthKit/PhotoKit) |
| 14 | Web console | IMPLEMENTED for the §48 view set; view depth unverified |
| 15 | Approvals / audit | PARTIAL |

### 1. Resource registry (PARTIAL)

- Entity/relationship model: `EntityService` at
  [backend/knowledge/entity.py:139](backend/knowledge/entity.py:139) with
  `find_by_alias` [:292](backend/knowledge/entity.py:292), `list_entities`
  [:312](backend/knowledge/entity.py:312), `add_alias`
  [:353](backend/knowledge/entity.py:353).
- Stable id + SHA-256 content hash: `content_hash` at
  [schemas/base.py:34](schemas/base.py:34); `Record` with stable `id`
  [schemas/base.py:57](schemas/base.py:57), [:76](schemas/base.py:76).
- No URI identity/aliases at the resource level: `resource_uri` / `urn:`
  grep in `backend/` + `schemas/` = 0.

### 2. Connector SDK (PARTIAL)

- `Connector` ABC at [backend/connectors/base.py:524](backend/connectors/base.py:524)
  with `connect` [:631](backend/connectors/base.py:631), `disconnect`
  [:670](backend/connectors/base.py:670), `health_check`
  [:894](backend/connectors/base.py:894), `snapshot`
  [:697](backend/connectors/base.py:697), `subscribe`
  [:762](backend/connectors/base.py:762).
- `credential_ref` [:591](backend/connectors/base.py:591); checkpoint is
  `last_synced_at` only [:595](backend/connectors/base.py:595) — no cursor
  pagination (`cursor` grep in the file = 0).
- `RetryPolicy` [:116](backend/connectors/base.py:116), `HTTPPool`
  [:257](backend/connectors/base.py:257) (introduced in the module docstring
  at :21–28).
- Concrete connectors: `GitConnector`
  [backend/connectors/git.py:152](backend/connectors/git.py:152),
  `GPUConnector` [backend/connectors/gpu.py:52](backend/connectors/gpu.py:52),
  `HostConnector` [backend/connectors/host.py:64](backend/connectors/host.py:64),
  `KubernetesConnector`
  [backend/connectors/kubernetes.py:25](backend/connectors/kubernetes.py:25).
- **MUST-NOT-REWRITE:** `ConnectorRegistry`
  [backend/connectors/registry.py:110](backend/connectors/registry.py:110)
  (register/create/`registered_types` [:466](backend/connectors/registry.py:466));
  the lazy-import defect is in `make_default_registry` only (see Hard Defects).

### 3. Event fabric (IMPLEMENTED — custom envelope, NOT CloudEvents)

- JetStream publish: `publish` at
  [backend/events/publisher.py:126](backend/events/publisher.py:126),
  `js.publish` [:168–169](backend/events/publisher.py:168); trace propagation
  [:304–309](backend/events/publisher.py:304).
- Envelope + replay types (module docstring at :1–26):
  `EventEnvelope` [backend/events/models.py:215](backend/events/models.py:215),
  `ReplayRequest` [:309](backend/events/models.py:309), `ReplayResult`
  [:335](backend/events/models.py:335), `DeliveryMetadata`
  [:66](backend/events/models.py:66).
- Idempotency: wire field `idempotency_key`
  [schemas/events.py:108](schemas/events.py:108); unique constraint
  [backend/persistence/alembic/versions/001_initial.py:73](backend/persistence/alembic/versions/001_initial.py:73).
- Durable JetStream pull consumer with in-process dead-letter list:
  `EventSubscriber` [backend/events/subscriber.py:82](backend/events/subscriber.py:82),
  DLQ init [:125](backend/events/subscriber.py:125), append
  [:341](backend/events/subscriber.py:341), count
  [:420](backend/events/subscriber.py:420).
- NATS client: `NatsClient`
  [backend/events/nats_client.py:189](backend/events/nats_client.py:189),
  `jetstream` property [:304](backend/events/nats_client.py:304).
- **MUST-NOT-REWRITE:** the whole `backend/events/` fabric plus the `events`
  table migration; consumers depend on the envelope shape and the
  idempotency_key uniqueness.

### 4. Rules / triggers (PARTIAL)

- Routing rules: `rules` field at
  [backend/inference/policy.py:121](backend/inference/policy.py:121)
  (`PolicyRule` [:71](backend/inference/policy.py:71));
  `InferencePolicyEngine` [:95](backend/inference/policy.py:95).
- Derivation rules: `DerivationRule`
  [backend/knowledge/derivation.py:46](backend/knowledge/derivation.py:46).
- Loop protection per agent: `loop_protection` field
  [backend/agents/registry.py:80](backend/agents/registry.py:80), first
  per-agent config [:205](backend/agents/registry.py:205) (configs at
  :205–269).
- No declarative predicate language; no debounce/cooldown
  (`debounce|cooldown` grep in `backend/` = 0).

### 5. Durable workflows (PARTIAL)

- `DAGScheduler` [backend/scheduler/scheduler.py:102](backend/scheduler/scheduler.py:102).
- Task DAG: `TaskDAG` [backend/tasks/dag.py:63](backend/tasks/dag.py:63)
  (`topological_order` [:264](backend/tasks/dag.py:264), `RunnableQueue`
  [:322](backend/tasks/dag.py:322)).
- Run-history router: [backend/api/endpoints/runs.py:5](backend/api/endpoints/runs.py:5)
  (prefix `/api/runs`).
- Append-only blackboard: `Blackboard`
  [backend/agents/registry.py:120](backend/agents/registry.py:120).
- No Temporal or equivalent: the 48 `temporal` hits in `backend/` are
  temporal-decay logic (e.g.
  [backend/context/prioritization.py:63](backend/context/prioritization.py:63)),
  not a workflow engine.

### 6. Functions / OCI sandbox (ABSENT)

- `sandbox|firecracker|wasm` grep in `backend/` = 0.

### 7. IAM PDP (PARTIAL — scaffold, functional no-op)

- `PermissionEnforcer`
  [backend/policy/permissions.py:39](backend/policy/permissions.py:39);
  `default_level` [:72](backend/policy/permissions.py:72).
- **`_get_subject_level` ignores subject and resource and always returns
  `default_level`** [:93–95](backend/policy/permissions.py:93).
- Re-exported only
  ([backend/policy/__init__.py:14](backend/policy/__init__.py:14)); 0
  callers in `backend/api/`. No Cedar (`cedar` grep = 0).
- Real authentication is JWT/SSO:
  [backend/api/auth.py:1–5](backend/api/auth.py:1) (SSO/OIDC discovery
  [:6–8](backend/api/auth.py:6), break-glass login).
- **MUST-NOT-REWRITE:** the JWT/SSO layer in `backend/api/auth.py`
  + `backend/api/auth_service.py` (AC-S01/S03/S05 endpoints).

### 8. Secrets (PARTIAL)

- DB-URL redaction workaround: factory-defect docstring
  [backend/api/wiring.py:29–36](backend/api/wiring.py:29); `_safe_redact`
  [:83–88](backend/api/wiring.py:83).
- No vault (`vault` grep in `backend/` + `mcp/` = 0).
- Insecure default shipped but unwired: admin/password user store
  [mcp/auth.py:9](mcp/auth.py:9).

### 9. Observability (PARTIAL)

- OTel + Prometheus: imports
  [backend/observability/__init__.py:28–31](backend/observability/__init__.py:28);
  `Metrics` [:44](backend/observability/__init__.py:44);
  `record_inference` [:173](backend/observability/__init__.py:173).
- Dependencies: [pyproject.toml:35–38](pyproject.toml:35).
- Endpoint: router prefix `/observability`
  [backend/api/observability.py:16](backend/api/observability.py:16);
  trace lookup [:88](backend/api/observability.py:88).
- No Grafana/Loki assets in `deployments/` (only spec text
  [docs/nervcenter-spec.md:1739](docs/nervcenter-spec.md:1739)).

### 10. LocalStack / object storage (LocalStack ABSENT at C0; probe lab lives in `tools/localstack/`)

- No LocalStack emulator is deployed in the in-tree code path (only the S3
  SigV4 client + memory backend are wired). "LocalStack" appears in the
  in-tree backend sources only in docstrings:
  [backend/storage/s3.py:6](backend/storage/s3.py:6) and
  [:25](backend/storage/s3.py:25).
- The C0 probe lab lives in-tree at
  [tools/localstack/probe.py](tools/localstack/probe.py) (691 lines),
  [tools/localstack/probe-results.jsonl](tools/localstack/probe-results.jsonl)
  (19 records), and [tools/localstack/README.md](tools/localstack/README.md)
  (152 lines). The emulator itself remains external to this repo — see
  "LocalStack probe — RECORDED EVIDENCE" at the end of this file.
- S3 SigV4 client: `S3ObjectStore`
  [backend/storage/s3.py:95](backend/storage/s3.py:95),
  `_canonical_request` [:150](backend/storage/s3.py:150), `_sign`
  [:167](backend/storage/s3.py:167), `Authorization` header
  [:200](backend/storage/s3.py:200).
- Memory backend: `InMemoryObjectStore`
  [backend/storage/memory.py:41](backend/storage/memory.py:41).
- Tests: [tests/backend/test_object_storage.py:21](tests/backend/test_object_storage.py:21)
  (SigV4 round-trip against an in-memory emulator via `httpx.MockTransport`).
- **MUST-NOT-REWRITE:** the object-store contract
  (`backend/storage/contracts.py`) and the "honest-failure" rules documented
  in [backend/storage/s3.py:9–24](backend/storage/s3.py:9).

### 11. Attention queue (ABSENT)

- `attention` grep in `backend/` = 0; in `tests/` = 0. The abstraction is
  only specified in the addendum [prompt2.md](prompt2.md) (ATTENTION section,
  §1–2); no backend implementation exists.

### 12. Ideas / goals (PARTIAL)

- `GoalService` [backend/goals/service.py:129](backend/goals/service.py:129).
- Reconciliation is task-progress-only: `GoalReconciler`
  [backend/goals/reconciliation.py:157](backend/goals/reconciliation.py:157);
  "ground truth is the actual state of the tasks"
  [:3–6](backend/goals/reconciliation.py:3).
- MCP tools: `get_goal` [mcp/tools_module.py:173](mcp/tools_module.py:173),
  `create_goal` [:182](mcp/tools_module.py:182), `update_goal`
  [:197](mcp/tools_module.py:197).

### 13. Photos / health / Apple connectors (ABSENT)

- `apple/` contains [apple/CONTRACT_NOTES.md](apple/CONTRACT_NOTES.md),
  Watch (`Watch/Store/WatchDataStore.swift`,
  `Watch/Store/WatchSnapshotSource.swift`), Widgets
  (`apple/Widgets/NerveCenterWidgetBundle.swift` + 5 view/provider files), and
  an iOS app shell (`apple/iOS/App/` — 12 Swift files:
  `NervePhoneApp.swift`, 6 views, `PhoneFetchService.swift`,
  `PhoneDataStore.swift`, `WireModels.swift`).
- `HealthKit|PhotoKit` grep in `apple/` = 0 — no photo or health
  integration; widget/watch data sources are in-memory or snapshot-based.

### 14. Web console (IMPLEMENTED for the §48 view set)

- Spec §48 ("Web UI", [docs/nervcenter-spec.md:1866](docs/nervcenter-spec.md:1866))
  lists 10 primary views. All 10 exist under `web/src/views/` and are routed
  in [web/src/App.tsx:37–46](web/src/App.tsx:37)
  (Overview, Systems, Timeline, Goals, TaskDag, Agents, Inference, Incidents,
  Decisions, Ask) plus `LoginView`
  ([web/src/App.tsx:26](web/src/App.tsx:26)). React 18 + Vite + TypeScript
  ([web/package.json:19–35](web/package.json:19)).
- View depth (real data vs mock) is Unverified from source alone:
  `web/src/api/mockClient.ts` exists alongside
  [web/src/api/client.ts](web/src/api/client.ts).
- **MUST-NOT-REWRITE:** route table in `web/src/App.tsx` and
  `web/src/api/client.ts` contract usage.

### 15. Approvals / audit (PARTIAL)

- Schemas: `Approval` [schemas/approvals.py:28](schemas/approvals.py:28)
  (approver types + decision literals at :22, :25); `Evidence`
  [schemas/evidence.py:53](schemas/evidence.py:53),
  `EvidenceCaptureRequest` [:92](schemas/evidence.py:92).
- Service: `EvidenceService`
  [backend/evidence/service.py:101](backend/evidence/service.py:101).
- API: approvals read router
  [backend/api/endpoints/approvals.py:6](backend/api/endpoints/approvals.py:6);
  approval verify endpoints (AC-S05) referenced in
  [backend/api/auth.py:14–15](backend/api/auth.py:14); changes router
  [backend/api/endpoints/changes.py:5](backend/api/endpoints/changes.py:5).
- No dedicated audit-trail service: "audit" hits in `backend/` are
  comments (e.g. [backend/persistence/alembic/versions/003_tasks_dependencies.py:1](backend/persistence/alembic/versions/003_tasks_dependencies.py:1)),
  guardrail provenance checks
  ([backend/policy/guardrails.py:306](backend/policy/guardrails.py:306)), and
  `PermissionCheck.to_check`
  ([backend/policy/permissions.py:118](backend/policy/permissions.py:118)).

### Client / SDK state

- `tests/backend/` (16 files) covers events
  ([tests/backend/test_events_api.py:178](tests/backend/test_events_api.py:178)
  et seq.), tasks ([tests/backend/test_tasks_dependencies.py](tests/backend/test_tasks_dependencies.py)),
  agents, orchestration, persistence, auth/SSO, object storage,
  observability. `tests/mcp/` covers tools + wire.
- Attention and photo/health: zero test coverage (grep = 0 in `tests/`).
- SDKs partial: Python
  (`sdk/python/nervcenter_sdk/client.py`, `groups.py`; tests at
  [sdk/python/tests/test_nervcenter_sdk.py](sdk/python/tests/test_nervcenter_sdk.py));
  TypeScript (`sdk/typescript/src/client.ts`, 353 lines, built `dist/`).
- CLI: `cli/` with command modules
  ([cli/commands/](cli/commands/)), trace header sent at
  [cli/client.py:116](cli/client.py:116).
- API versioning: `X-API-Version` middleware
  [backend/api/versioning.py:45–49](backend/api/versioning.py:45).

### Deployment state

- Kubernetes manifests: [deployments/kubernetes/base/](deployments/kubernetes/base/)
  (api, mcp, web, oauth deployments+services; `nats-deployment.yaml`,
  `nats-service.yaml`; `postgres-statefulset.yaml` + `postgres-pvc.yaml`;
  `ingress.yaml`; `migrate-job.yaml`; `secrets.yaml.example`).
- Local: [deployments/local/docker-compose.yaml](deployments/local/docker-compose.yaml)
  (services at :19 postgres, :36 nats, :51 migrate, :66 api, :94 web).
- Nix: [deployments/nix/default.nix](deployments/nix/default.nix);
  Harbor: [deployments/harbor/deploy.sh](deployments/harbor/deploy.sh).
- **Dead reference:** `NERVE_OMNIROUTE_URL`
  ([deployments/kubernetes/base/secrets.yaml.example:31–34](deployments/kubernetes/base/secrets.yaml.example:31))
  points at `omniroute.nervecenter.svc:8080`, but no OmniRoute K8s
  service/deployment manifest exists; compose sets
  `NERVE_OMNIROUTE_URL: http://omniroute:8080`
  ([deployments/local/docker-compose.yaml:78](deployments/local/docker-compose.yaml:78))
  with no matching service either.

### OmniRoute / inference wiring state

- `OmniRouteAdapter` [backend/inference/omniroute.py:60](backend/inference/omniroute.py:60),
  `route()` [:94](backend/inference/omniroute.py:94), fallback hop loop
  [:121–122](backend/inference/omniroute.py:121), exhaustion
  `RuntimeError` [:136](backend/inference/omniroute.py:136).
- Schemas: `RouteHop` [schemas/routes.py:36](schemas/routes.py:36),
  `InferenceRoute` [:53](schemas/routes.py:53),
  `OmniRouteTelemetry` [:104](schemas/routes.py:104).
- `LatencyWindow`
  [backend/inference/telemetry.py:32](backend/inference/telemetry.py:32).
- Adapters: `InferenceAdapter`
  [backend/inference/adapter.py:40](backend/inference/adapter.py:40),
  Local :92, OpenAI :102, Anthropic :119, HuggingFace :136,
  `AdapterPool` [:151](backend/inference/adapter.py:151).
- **The inference package is not imported anywhere under `backend/api/`**
  (0 import statements); the only coupling is the service-lookup key
  `service_name="inference"`
  ([backend/api/endpoints/routes.py:8](backend/api/endpoints/routes.py:8)).
- MCP: JSON-RPC 2.0 wire (module docstring
  [mcp/core.py:3](mcp/core.py:3)); `dispatch_message`
  [mcp/core.py:50](mcp/core.py:50); 18 tools in `_SPEC`
  ([mcp/tools_module.py:134–322](mcp/tools_module.py:134),
  `build_tools` [:337](mcp/tools_module.py:337)); HTTP endpoint
  `POST /mcp` [mcp/core.py:270–271](mcp/core.py:270).
- `Nerve-Trace-Id` sent by [mcp/api.py:17](mcp/api.py:17) and
  [cli/client.py:23](cli/client.py:23); **0 reads in `backend/api/`**;
  trace propagation happens only at NATS publish time
  ([backend/events/publisher.py:304–309](backend/events/publisher.py:304)).
- LocalStack probe — **RECORDED EVIDENCE** (live run 2026-10-10; all facts
  below cite [tools/localstack/probe-results.jsonl](tools/localstack/probe-results.jsonl)
  per-record; scope and credential rules in
  [tools/localstack/README.md](tools/localstack/README.md)):
  - LocalStack live at `http://localhost:4566` on 2026-10-10; health ok:
    records 1–4 (`env-endpoint=configured/default`, `env-token=not-set`,
    `env-python=ok` 3.13.15, `endpoint-health=available/http-200`,
    `features.persistence=initialized`).
  - `/_localstack/plan` → 404 → plan tier Unknown: record 5
    (`plan-endpoint=unknown/http-404`); record 6
    (`plan-info-fallback=server-claim/http-200`) reports
    `server_version=2026.3.0:e28f8cfa4`, `server_edition_claim="pro"`,
    `server_license_activated_claim=true` explicitly as a self-report, not a
    plan-tier verification.
  - SQS/DynamoDB/IAM/CloudFormation available: records 11, 14, 15, 16
    (`service-sqs`, `service-dynamodb`, `service-iam`,
    `service-cloudformation`, all `available/http-200`); record 4's health map
    also lists `sqs`, `dynamodb`, `iam`, `cloudformation`, `s3`, `lambda`,
    `sts` as `running`.
  - SNS disabled by SERVICES: record 4 lists `sns: disabled`; record 12
    (`service-sns=unavailable/InternalFailure`).
  - S3/Lambda/EventBridge/StepFunctions Unknown pending boto3: record 7
    (`boto3-presence=boto3-missing/ModuleNotFoundError`), records 8, 9, 10, 13
    (`service-s3`, `service-lambda`, `service-events`,
    `service-stepfunctions`, all `unknown/http-200-empty-body`).
  - ENFORCE_IAM no-denial: record 17
    (`enforce-iam=no-denial/created-and-cleanup-ok`).
  - CLI absent: record 18 (`cli-presence=absent/shutil.which-miss`); CLI
    subcommands Unknown: record 19
    (`cli-subcommands=unknown/cli-binary-not-on-PATH`).
  - Verified in-repo code path remains only the S3 SigV4 client and memory
    backend (capability 10); the emulator surface is exercise-pending.

## Existing Internal Spec

The governing baseline is [docs/nervcenter-spec.md](docs/nervcenter-spec.md)
(3,566 lines). Anchor sections: §1 Executive Summary
([:14](docs/nervcenter-spec.md:14)); §15 Evidence System
([:839](docs/nervcenter-spec.md:839)); §46 HTTP API
([:1793](docs/nervcenter-spec.md:1793)); §47 CLI
([:1831](docs/nervcenter-spec.md:1831)); §48 Web UI
([:1866](docs/nervcenter-spec.md:1866)); §52 Apple Platform
([:1970](docs/nervcenter-spec.md:1970)). The personal-operations/attention
addendum lives in [prompt2.md](prompt2.md) (v0.1, extends the parent spec) and
is not yet implemented (capability 11). Workstream tracking:
[docs/implementation-status.yaml](docs/implementation-status.yaml)
(spec §95, last updated 2026-09-29). NC-SPEC-2026-010 is the
AWS-like-local-cloud successor spec; this inventory is its C0
current-state baseline.

## Hard Defects

1. **Dead registry imports** —
   `make_default_registry` lazily imports
   `from .nix` [:501](backend/connectors/registry.py:501),
   `from .omniroute` [:502](backend/connectors/registry.py:502),
   `from .zoo` [:503](backend/connectors/registry.py:503); none of
   `backend/connectors/{nix,omniroute,zoo}.py` exists, so calling
   `make_default_registry`
   ([backend/connectors/registry.py:477](backend/connectors/registry.py:477))
   raises `ModuleNotFoundError`. Latent today: zero callers repo-wide.
2. **Unwired inference** — `backend.inference` has 0 imports under
   `backend/api/`; policy engine, adapters, and OmniRoute routing exist but
   are unreachable from the HTTP API (only the `service_name="inference"`
   lookup key at [backend/api/endpoints/routes.py:8](backend/api/endpoints/routes.py:8)).
3. **Unauthenticated MCP** — `POST /mcp`
   ([mcp/core.py:270–271](mcp/core.py:270)) has no auth middleware; the
   `mcp/auth.py` token store is never imported by the MCP app, and its
   default credential `admin`/`password` is at
   [mcp/auth.py:9](mcp/auth.py:9).
4. **Unread Nerve-Trace-Id** — sent by MCP
   ([mcp/api.py:17](mcp/api.py:17)) and CLI
   ([cli/client.py:23](cli/client.py:23)) per spec §29, but 0 readers in
   `backend/api/`; correlation is dropped at the API boundary.
5. **No-op PermissionEnforcer** —
   `_get_subject_level` ignores `subject` and `resource` and returns
   `default_level`
   ([backend/policy/permissions.py:93–95](backend/policy/permissions.py:93)),
   so every `check()` passes for any subject at or below the default level.
6. **Absent PDP wiring** — `PermissionEnforcer` is re-exported
   ([backend/policy/__init__.py:14](backend/policy/__init__.py:14)) but
   invoked nowhere in the API; no Cedar policy engine exists, so the real
   authorization path is JWT/SSO only
   ([backend/api/auth.py:1–5](backend/api/auth.py:1)) with no
   resource-level policy decision point.

## Open Questions

1. **Sibling repos `omniroute` / `zoo`** — Unknown. Not present in this
   checkout and not referenced by any in-repo path; the connector modules
   they would back are the missing `nix`/`omniroute`/`zoo` imports in
   Hard Defect 1.
2. **LocalStack plan tier** — Unknown. The 2026-10-10 live probe found
   `/_localstack/plan` returning 404 (record `plan-endpoint:5` in
   [tools/localstack/probe-results.jsonl](tools/localstack/probe-results.jsonl));
   the artifact is in-tree (19 records) and verified.
3. **LocalStack licensing** — Unknown (no license evidence in repo; the
   server self-reports edition "pro" per probe record
   `server_version:6`, which is a claim, not a verified entitlement).
4. **OmniRoute deployment** — `NERVE_OMNIROUTE_URL` is a dead reference in
   both K8s ([deployments/kubernetes/base/secrets.yaml.example:32](deployments/kubernetes/base/secrets.yaml.example:32))
   and compose
   ([deployments/local/docker-compose.yaml:78](deployments/local/docker-compose.yaml:78));
   deciding whether to add an OmniRoute service or remove the env var is
   open.

## Since the 2026-10-10 baseline

The inventory above is the frozen C0 current-state (2026-10-10). The items
below are material changes that landed after that snapshot and are recorded
here for continuity; the machine-readable form of the same items lives in
[`docs/implementation-status.yaml`](../../docs/implementation-status.yaml)
(`recent_activity`, dated 2026-10-19).

1. **FastAPI `on_event` retired (G-4.2 / E1).** The startup/shutdown hooks at
  [`backend/api/wiring.py`](../../backend/api/wiring.py) are now a single
  `@asynccontextmanager` lifespan registered on the router's
  `lifespan_context` (startup: `wire_dependencies`; shutdown:
  `teardown_dependencies`); the deferral comment is gone. No FastAPI
  `@app.on_event` / `@target.on_event` decorator usage remains in
  `backend/api` (the only remaining `on_event` strings in `backend/` are the
  connector's unrelated `on_event` callback parameter in
  `backend/connectors/*`, a different API, out of E1 scope).
  `tests/backend/test_app_wiring.py` is 9/9 green with **zero** `on_event`
  deprecation warnings.
2. **Apple test-truth corrected (E2 / D3).** The canonical Apple suite is
  **46** tests, not 31: NerveSharedTests 13 / NerveRelayTests 31 /
  NerveAppsTests 2. The earlier "31" was NerveRelayTests only, a symptom of
  the stale-test-product under-run trap now guarded by
  [`apple/scripts/test-apple.sh`](../../apple/scripts/test-apple.sh), which
  asserts the per-bundle counts (and doubles as the cold-cache guard).
3. **Watch bundle-ID family converged (E2 / D2).** The watch app and its
  widget extension now use the
  `com.celesrenata.NervePhone.watch` family
  (`com.celesrenata.NervePhone.watch` app /
  `com.celesrenata.NervePhone.watch.widgets` extension), declared in
  [`apple/Distribution/xcodegen/NerveWatch.xcodegen.yml`](../../apple/Distribution/xcodegen/NerveWatch.xcodegen.yml)
  and regenerated under `apple/Distribution/Generated/`.
4. **Personal surface plane: data lift landed, surfaces still open (E2).**
  The 2026-10-18 lift makes `NervePhone` render real server data on load
  (`PhoneDataStore.fetchNow`; `--nerve-server` test override; App Group
  `cached-state.json` verified with live values). See
  [`docs/apple-data-lift-2026-10-18.md`](../../docs/apple-data-lift-2026-10-18.md).
  **Still open:** the Personal Operations Plane surfaces
  (Today/Inbox/People/Waiting/Briefing) and the `Today | Engineering | Ask`
  reorg — `apple/iOS/App/Views` currently ships only
  Overview/Agents/Goals/Incidents/Systems/Ask, so capability 11 remains
  unimplemented as of this baseline.
5. **Commit-hygiene norm seconded (E5).** The two-clause commit-hygiene norm
  from gaps2 §7 Q6 (a generated project and its patcher ship together; a
  backend import-chain commit is verified by a fresh-worktree import) is now
  a standing rule in [`specs/phase0-integrity/tasks.md`](../../specs/phase0-integrity/tasks.md)
  and [`specs/t001-stabilization/tasks.md`](../../specs/t001-stabilization/tasks.md).
6. **Python suite baseline is 601.** `pytest tests/ -m "not integration" -q`
  → 601 passed, 0 failed (2026-10-19); the canonical test commands are
  documented in [`README.md`](../../README.md) §Testing.
