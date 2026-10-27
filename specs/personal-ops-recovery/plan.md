# Personal Operations Recovery — Execution Plan

## Architecture decisions

1. Extend existing components; use repository-backed persistence instead of in-memory personal singletons. The audited durable boundary is Async SQLAlchemy, Alembic, and Postgres ([`persistence-infra.md`](audit/persistence-infra.md:13)); app wiring is intentionally optional today ([`persistence-infra.md`](audit/persistence-infra.md:208)).
2. Establish a linear Alembic 006–010 chain. The migration Job upgrades before API rollout ([`contracts-evidence.md`](audit/contracts-evidence.md:230)).
3. Run a restart-safe ingest supervisor from FastAPI lifespan; DB-owned cursors/jobs are the source of truth.
4. Enforce the frozen Connection Center axes, encrypted secret references, local-first PDP inference, and privacy rules in [`spec.md`](spec.md:32).
5. F owns Wave-1 [`backend/api/wiring.py`](../../backend/api/wiring.py:1) edits. Later workstreams register through `backend/persistence/repositories/registry.py`; they must not edit wiring.

## File ownership — non-overlapping

| Workstream | Owned paths |
| --- | --- |
| A | `web/src/views/ConnectionsView.tsx`, `web/src/components/connections/*`, `web/src/lib/api/connections.ts`, router/nav edits in `web/src/App.tsx` + `web/src/components/Layout.tsx`; L owns `/`, A adds `/connections` nav |
| B | `backend/api/endpoints/connections.py`, `backend/api/endpoints/relay.py`, `backend/connectors/registry.py`, `backend/connectors/vault.py`, `backend/persistence/models/connections.py` |
| C | `backend/ingest/__init__.py`, `backend/ingest/supervisor.py`, `backend/ingest/mail_worker.py`, `backend/connectors/mail/`, extend-only `backend/api/endpoints/mail_connectors.py` |
| D | `apple/Relay/` CLI/plist/pair flow and `apple/Relay/INSTALL.md`; helpers in B's `relay.py` only if frozen routes are insufficient, flagged in review |
| E | `backend/calendar/`, `backend/persistence/models/calendar.py`, extend-only `backend/api/endpoints/calendar.py` |
| F | `backend/persistence/models/personal.py`, new `backend/persistence/repositories/`, `backend/persistence/alembic/versions/006_personal_plane.py`, `backend/api/wiring.py`, `tests/backend/test_restart_persistence.py`; owns wiring only in Wave 1 |
| G | `backend/bills/`, `backend/persistence/models/bills.py`, `backend/api/endpoints/bills.py`, `web/src/views/BillsView.tsx`, `backend/persistence/alembic/versions/007_bills.py` |
| H | `backend/routines/`, `backend/persistence/models/routines.py`, `backend/api/endpoints/routines.py`, `web/src/views/RoutinesView.tsx` or Today section, `backend/persistence/alembic/versions/008_routines.py` |
| I | `backend/wellness/`, `backend/persistence/models/wellness.py`, `backend/api/endpoints/wellness.py`, `web/src/views/WellnessView.tsx`, `backend/persistence/alembic/versions/009_wellness.py` |
| J | registry methods only plus new `backend/communications/relationships_service.py`, extend `backend/api/endpoints/people.py` + `backend/api/endpoints/relationships.py`, `web/src/views/PeopleView.tsx` |
| K | `backend/attention/` with `aggregator.py` and `escalation.py`, extend-only `backend/api/endpoints/attention.py`, notification-log writes |
| L | `web/src/views/TodayView.tsx`, `web/src/lib/api/today.ts`, `backend/api/endpoints/today.py`, and `/` default landing in `web/src/App.tsx` |
| M | `apple/iOS/App/`, `apple/Watch/`, extensions to `apple/Shared/Sources/NerveAPI/` + `NerveModels/` excluding frozen `Event.swift`, `apple/Widgets/` |
| N | new `tests/e2e/`, extension to F's restart test, new `specs/personal-ops-recovery/privacy-review.md`, and `web/tests/`; only N may add MCP tools with the count test updated in one place |

Shared forbidden paths are enumerated in [`spec.md`](spec.md:93). Existing mail/calendar/attention routes remain compatible.

## Waves and gates

| Wave | Parallel work | Entry gate | Exit gate |
| --- | --- | --- | --- |
| 1 | F, B, C | Freeze accepted; B registry/control-plane contract available to C | Disjoint patches; backend suite green; web tests/build green; rows 1–4 and 2 covered |
| 2 | A, D, E, L | Wave 1 integrated; A consumes B, D consumes relay, E uses registry/vault, L uses F tables | Same suite gates; rows 5, 7, 15 covered |
| 3 | G, H, I, J | Wave 1 personal repositories integrated | Same suite gates; rows 8–9 covered |
| 4 | K, M, N | Waves 1–3 integrated | Same suite gates; rows 4, 8, 10–14 covered |
| Integration | Parent/orchestrator | All reviewed, conflict-free patches | Full gate and acceptance sweep below, then commit |

Every workstream exit includes its acceptance-row tests; no worker may claim an unrun check as passing.

## Acceptance mapping

| Row | Workstreams | Verification focus |
| --- | --- | --- |
| 1 | A+B+C | Two email accounts through UI |
| 2 | F+B | Connection/config restart persistence |
| 3 | C | Automatic new mail ingestion |
| 4 | C+K | Deadline mail becomes action |
| 5 | D+A | Guided Mac Relay setup |
| 6 | D+C-style ingest | Durable relay message ingestion |
| 7 | E+L | Appointment projected to Today |
| 8 | G+K | Due-date escalation |
| 9 | F+J | Durable, completable commitment |
| 10 | M+K | Important priority on iPhone/Watch |
| 11 | K+L+M | Snooze, complete, dismiss everywhere |
| 12 | N + PDP policy | No accidental disclosure |
| 13 | B+A | Actionable diagnostics |
| 14 | N | No demo/sample e2e dependency |
| 15 | A | Five visible connection states |

## Risk register

| ID | Risk / decision | Treatment |
| --- | --- | --- |
| F2 | NATS JetStream uses `emptyDir`, losing in-cluster history on restart ([`contracts-evidence.md`](audit/contracts-evidence.md:255)). | Out of scope; document residual risk. |
| F3 | MCP Tool schema has no permission field ([`contracts-evidence.md`](audit/contracts-evidence.md:259)). | Out of scope; do not claim permissioned tool metadata. |
| F4 | Existing write-plane 503s are intentional ([`contracts-evidence.md`](audit/contracts-evidence.md:263)). | Out of scope; preserve honesty semantics. |
| F5 | API image is unpinned ([`contracts-evidence.md`](audit/contracts-evidence.md:267)). | Unverified deployment concern; no scope expansion. |

## Integration procedure

1. Parent inspects each isolated patch for ownership conflicts and frozen-path violations before application.
2. Apply waves in order; resolve only ownership-approved integration changes.
3. Run `uv run pytest -m "not integration"`, then `cd web && npm test` and `npm run build`.
4. Run the restart test, execute acceptance rows 1–15, record evidence, and confirm no demo seeds are necessary.
5. Verify migration Job ordering and run Alembic upgrade in the deployment procedure; commit only after all gates pass.
