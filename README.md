# NerveCenter

NerveCenter is a personal intelligence system: a local-first, event-driven core that
captures evidence, maintains a structured knowledge graph, tracks goals, orchestrates
agents, and exposes the results through APIs, MCP tools, SDKs, a CLI, a web UI, and
native Apple clients.

As of 2026-11-16 the core has grown well past the original spec's 15-module layout:

- **NerveShield** — a zero-trust mail security plane (`backend/mail_security`, ~50
  modules; spec + 110 ADRs in `.kiro/specs/nerve-shield/`). Shadow-mode verdicts,
  authenticated-evidence handling, a Granite semantic observer, campaign/expectedness
  SHADOW analytics, ClamAV attachment scanning, and a canary promotion gate
  (REVIEW_GATED, per-account) are live; the review queue, task-proposal and
  confirmation workflows are built but OFF by default.
- **Personal life plane** — bills (incl. mail-to-bill proposals), finance/debt,
  obligations, routines, wellness, Apple Health bridge, mental health (PHQ-9/GAD-7),
  research (durable long-term researcher), and handoffs (central opencode + Mac agent).
- **Inference policy (NS-ADR-107)** — local models first; OpenAI only for
  owner-requested/high-stakes/local-failed work, behind a single slow limiter
  (concurrency 1, 20 s spacing, hourly/daily caps).

Live workstream tracking lives in [`docs/implementation-status.yaml`](docs/implementation-status.yaml)
and NerveShield decisions in [`.kiro/specs/nerve-shield/decisions.md`](.kiro/specs/nerve-shield/decisions.md).

## Design Principles

- **Local-first**: All state lives in an on-device database; cloud is optional.
- **Event-driven**: A single event bus drives every subsystem; nothing polls.
- **Evidence-based**: Every inference and decision cites captured evidence.
- **Schema-first**: Interfaces and stored records are validated against versioned schemas.
- **Multi-client**: One core, many surfaces — API, MCP, CLI, web, iOS, and watchOS.

## Repository Layout

The layout below started from Section 8 (Repository Structure) of the NerveCenter
specification and has since grown (see the module table further down).

| Path | Purpose |
| --- | --- |
| `backend/` | Python core: API, event bus, knowledge, evidence, goals, tasks, context, orchestration, scheduler, agents (incl. runner/cadence/SkillHub), inference (local-first policy), policy, connectors, observability, persistence, plus NerveShield mail security and the personal plane (attention, communications, bills, finance, health, wellness, research, …) |
| `schemas/` | Versioned JSON Schema / data contracts for all records and interfaces |
| `sdk/` | Client SDKs: Python (`sdk/python`) and TypeScript (`sdk/typescript`) |
| `mcp/` | MCP server exposing NerveCenter tools to model clients (30 tools: spec §45 + personal + `enqueue_coding_handoff`) |
| `cli/` | Terminal client for the core |
| `web/` | Browser UI (React 18 + Vite + TypeScript, ~40 views) |
| `apple/` | Native Apple clients: `Shared`, `iOS`, `Watch`, `Widgets`, `Relay` (iMessage bridge daemon), `HandoffAgent` (Mac coding-handoff runner), `Tests` |
| `agents/` | Agent definitions, prompts, and capability manifests (placeholder — live manifests are in `backend/agents/`) |
| `deployments/` | Deployment definitions: `nix`, `kubernetes`, `local`, `harbor` |
| `synthetic-lab/` | Synthetic-data lab for testing and evaluation |
| `migrations/` | Database schema migrations (Alembic revisions live in `backend/persistence/alembic/`) |
| `tests/` | Cross-cutting integration and end-to-end tests |
| `inspector/` | NerveShield static deep-inspection sidecar (pikepdf/defusedxml; `Dockerfile.inspector`) |
| `docs/` | Specifications, architecture notes, ADRs, and implementation status |
| `.kiro/specs/` | NerveShield spec bundle (requirements, design, threat model, 110 NS-ADRs, phase-gated tasks, evidence) |
| `tools/` | Owner/probe tooling (e.g. LocalStack probe lab, Arc host inventory) |

## Backend Modules

The 15 original spec modules are all present, plus the NerveShield and personal-life
planes added afterwards. Grouped by function (41 modules total; verified in-tree 2026-11-16):

| Group | Module | Responsibility |
| --- | --- | --- |
| Foundation | `api` | HTTP surface: FastAPI app, JWT/SSO auth, ~63 endpoint routers, versioning, wiring |
| | `events` | Event bus (NATS JetStream), subscriptions, replay, DLQ |
| | `persistence` | Storage engines, WAL, snapshots, Alembic migrations |
| | `schemas` contracts | (in `schemas/`) versioned data contracts |
| | `storage` | Object store: S3 SigV4 client, encrypted `file://`/memory backends |
| | `observability` | Tracing (OTel), metrics (Prometheus), logs |
| Knowledge | `knowledge` | Knowledge graph, entities, relations, derivation |
| | `evidence` | Evidence capture, provenance, citations |
| | `registry` | Canonical resource registry (NC-SPEC-2026-010) |
| | `connectors` | External integrations: git, kubernetes, gpu, host, mail (IMAP/OAuth), OmniRoute observer, Vault |
| Planning | `goals` | Goal models, progress, reconciliation |
| | `tasks` | Task DAG, queues, execution |
| | `scheduler` | Time-based scheduling, DAG scheduler, reminders |
| | `orchestration` | Multi-agent orchestration, plan execution, incident workflow |
| | `agents` | Agent runtime, registries (14 native types), runner (cadence, back-off, Skill Hub) |
| Inference | `inference` | Model routing, adapters; local-first policy (NS-ADR-107), OpenAI limiter, secure client |
| | `context` | Context assembly, budgeting, grounded ask pipeline |
| | `policy` | Guardrails, permissions, redaction, PolicyDecisionPoint (R107 firewall) |
| Mail / security | `mail_security` | NerveShield: evidence capture, classifiers, threat intel, campaign/expectedness SHADOW analytics, review queue, promotion gate, ClamAV, static inspection, model registry |
| | `ingest` | Durable mail ingest: supervisor, workers, seen-tracking |
| Personal plane | `attention` | Attention engine (budget, quiet periods, escalation) |
| | `communications` | Cross-channel message normalization, commitment/request/deadline detection |
| | `contacts` | Contact resolution, identity merge, Mac/Google sync |
| | `messages` | Conversation threads |
| | `email` | Email store + service |
| | `voicemail` | Voicemail plane (relay probe) |
| | `calendar` | Calendar plane (Google via Gmail connection) |
| | `waiting_on` | Waiting-on tracking |
| | `followups` | Follow-up lifecycle |
| | `briefing` | Daily briefings |
| | `drafts` | Response/draft assistant (policy-gated) |
| | `obligations` | Unified obligations plane |
| | `bills` | Bills & bill candidates (mail → bill proposals) |
| | `finance` | Read-only financial helper: debt payoff, statement imports |
| | `health` | Apple Health bridge (validated ingest) |
| | `wellness` | Private wellness journal (mood, sleep) |
| | `mental_health` | PHQ-9/GAD-7 tracker, trends, correlations |
| | `projects` | Project context store, OpenAI engine, restricted project MCP |
| | `research` | Durable long-term researcher |
| | `handoffs` | Coding handoffs: central opencode + Mac agent |
| Platform | `services` | App services wired onto `app.state.services` |
| | `owner_tools` | Owner-run offline tools (backfills, exports, audits) — never imported by the service |

## Implementation Status

Live workstream tracking lives in [`docs/implementation-status.yaml`](docs/implementation-status.yaml).

## Development

```bash
# Python core tooling (ruff, pyright, pytest)
pip install -e ".[dev]"
ruff check .
pyright backend
```

## Testing

Canonical test commands (Phase 1, `gaps4.md` §A2). CI (`.github/workflows/ci.yml`)
runs the same three; a deliberately red test must fail CI.

| Plane | Command | Latest verified (2026-11-16) |
| --- | --- | --- |
| Python | `pytest tests/ -m "not integration"` | **9056 passed, 1 failed, 3 skipped, 12 deselected** (166 s) — the failure is the same one from 2026-11-09: `tests/backend/test_routines.py::TestWellnessSleepSeam::test_returns_iso_records_with_source` |
| Web | `cd web && npm ci && npm run build && npx playwright test` | **122 passed** (29 spec files; the 60-test figure is a stale baseline) |
| Apple | `bash apple/scripts/test-apple.sh` | **147 tests, 0 failures** (NerveSharedTests 13 / NerveRelayTests 100 / NerveAppsTests 34), asserted per bundle by the script |

Notes:

- The 2026-11-09 local run reported **7,524 passed, 1 failed**; the suite has since
  grown ~20% (NerveShield, agent runner, health/wellness/mental-health/finance,
  inference policy). The single failing test predates that growth — it was already
  red on 2026-11-09. Rerun the commands before treating any number here as release
  evidence; integration tests stay excluded by the `not integration` marker.
- The Apple script `touch`es the test sources, rebuilds with `swift build --build-tests`,
  runs `swift test`, and **asserts the per-bundle counts** — this defeats the
  stale-test-product under-run trap (`gaps4.md` §3.4, D3) and doubles as the cold-cache
  guard on a fresh macOS runner. `apple/build_and_test.sh` delegates to it.
- The Web command builds the production bundle before Playwright runs so E2E tests
  exercise the same artifact used for deployment.

## Deployment

Deployment targets and runbooks live in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), which covers the local, Kubernetes, and Nix deployment paths under `deployments/`.

## Documentation

- Specification and architecture notes: `docs/`
- Schemas and data contracts: `schemas/`
