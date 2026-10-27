# Persistence & Infrastructure Audit — Personal-Ops Recovery

**Status:** Read-only audit. No source, config, migration, or endpoint changes were made.
**Scope:** Five areas only — (1) durable ORM-backed persistence boundary, (2) in-memory /
ephemeral personal-plane state, (3) durable personal-plane state, (4) endpoint behavior when
durable storage is absent/unwired/unreachable, (5) future recovery / durability contract
(requirements only).
**Evidence basis:** Path inspection only. No tests, runtime probes, or migrations were run in
this audit.

---

## Area 1 — Durable ORM-backed persistence boundary

**Technology:** Async SQLAlchemy + asyncpg, Alembic-managed migrations, Postgres + pgvector.

### Connection / engine layer
- [`backend/persistence/connection.py`](backend/persistence/connection.py) is the persistence
  boundary module. Key constructs:
  - `DatabaseConfig` — immutable engine/pool configuration container
    (`@dataclass(frozen=True, slots=True)`, line ~63–96).
  - `config_from_env()` (line ~99–143) — builds `DatabaseConfig` from the process environment.
  - `redact_url(url)` (line ~146–160) — masks credentials before logging.
  - `create_engine(...)` (line ~163–205) — creates the async engine; optional `url` override.
  - `make_session_factory(engine)` (line ~208–221) — returns `async_sessionmaker[AsyncSession]`.
  - `create_persistence(...)` (line ~224–230) — top-level factory used at startup.
  - `session_scope(...)` (line ~233–256) — `@asynccontextmanager`; yields a session inside a
    transaction, commits on success, rolls back on error.
  - `ensure_pgvector(...)` (line ~259–270) — `CREATE EXTENSION IF NOT EXISTS vector` (best effort).
  - `pgvector_available(...)` (line ~277–294) — `SELECT 1 FROM pg_extension WHERE extname='vector'`
    probe returning `bool`.
  - `HealthReport` (line ~297–334) — structured result of `check_health`, with `as_dict()`.
  - `check_health(engine)` (line ~337–392) — probes the database on a single connection:
    - `SELECT 1` → connectivity + latency
    - `SELECT version()` → server banner
    - `SELECT 1 FROM pg_extension WHERE extname = 'vector'` → pgvector presence
    - Returns a `HealthReport` whose status is one of:
      - `healthy` — connected **and** `vector` extension present
      - `degraded` — connected but `vector` extension absent
      - `unhealthy` — could not obtain a connection
    - Also carries best-effort pool counters (`size` / `overflow` / `checkedout`, from
      `_snapshot_pool`, line ~395–406) and an `error` field.
  - `dispose_engine(engine)` (line ~409–416) — closes the pool at shutdown.

### Migration layer
- [`backend/persistence/migrate.py`](backend/persistence/migrate.py) — thin Alembic wrapper
  runnable as `python -m backend.persistence.migrate`.
  - `_script_location()` (line ~25–31) — returns `NERVE_ALEMBIC_SCRIPT_LOCATION` if set, else
    `backend/persistence/alembic` resolved relative to `migrate.py`.
  - `_build_config()` (line ~34–46) — builds an Alembic `Config()` programmatically with
    `script_location` + `sqlalchemy.url`; therefore **no `alembic.ini` on `$PATH` is required**.
    DB URL is taken from `NERVECENTER_DATABASE_URL` (fallback `ENV_DATABASE_URL`) with
    `DEFAULT_DATABASE_URL` as the final default.
  - `main(argv)` (line ~49–78) — default action is `upgrade` → `alembic upgrade head`; also
    supports `current` / `heads` / `history`.
- **Correct Alembic config path:** `backend/persistence/alembic.ini` (NOT a repo-root
  `alembic.ini`). The migrations are driven from `backend/persistence/alembic/versions/`.

### Migration chain (in order)
- [`001_initial.py`](backend/persistence/alembic/versions/001_initial.py)
- [`002_auth.py`](backend/persistence/alembic/versions/002_auth.py)
- [`003_tasks_dependencies.py`](backend/persistence/alembic/versions/003_tasks_dependencies.py)
- [`004_incidents_agents.py`](backend/persistence/alembic/versions/004_incidents_agents.py)
- [`005_nc_platform.py`](backend/persistence/alembic/versions/005_nc_platform.py) — adds the
  eight canonical resource platform tables (NC-SPEC-2026-010); header docstring
  ([`005_nc_platform.py:1–26`](backend/persistence/alembic/versions/005_nc_platform.py)) lists
  `nc_connector_instances`, `nc_resources`, `nc_resource_aliases`, `nc_resource_relations`,
  `nc_resource_snapshots`, `nc_principals`, `nc_policies`, `nc_approvals`.

### ORM model modules
Under [`backend/persistence/models/`](backend/persistence/models/):
`__init__.py`, `base.py`, `events.py`, `entities.py`, `relationships.py`, `evidence.py`,
`claims.py`, `tasks.py`, `goals.py`, `planned_changes.py`, `incidents_agents.py`, `auth.py`,
`nc_platform.py`.

`base.py` provides shared infrastructure: `Base`, `Vector`, `TimedRecord`, `RootRecord`, `Jsonb`,
`UUIDv4`.

### Durable table inventory (VERIFIED `__tablename__`)
All physical table names below were read directly from the model files; they are asserted as
verified, not inferred.

| Model module | ORM model(s) | Verified `__tablename__` |
| --- | --- | --- |
| `events.py` | `Event` | `events` |
| `entities.py` | `Entity` | `entities` |
| `relationships.py` | `Relationship` | `relationships` |
| `evidence.py` | `Evidence` | `evidence` |
| `claims.py` | `Claim`, `Decision` | `claims`, `decisions` |
| `tasks.py` | `Task`, `TaskRun` | `tasks`, `task_runs` |
| `goals.py` | `Goal`, `AgentResult` | `goals`, `agent_results` |
| `planned_changes.py` | `PlannedChange` | `planned_changes` |
| `incidents_agents.py` | `Incident`, `Agent` | `incidents`, `agents` |
| `auth.py` | `AuthSession`, `AuthDevice`, `PairingChallenge`, `ApprovalRecord` | `auth_sessions`, `auth_devices`, `pairing_challenges`, `approvals` |
| `nc_platform.py` | NC platform resources/principals/policies/approvals | `nc_resources`, `nc_connector_instances`, `nc_resource_aliases`, `nc_resource_relations`, `nc_resource_snapshots`, `nc_principals`, `nc_policies`, `nc_approvals` |

Cross-check: the migration `005_nc_platform.py` creates `nc_connector_instances` (docstring,
[`005_nc_platform.py:6`](backend/persistence/alembic/versions/005_nc_platform.py)), matching the
ORM `__tablename__` in `nc_platform.py` — confirming the ORM names reflect the physical schema.

---

## Area 2 — In-memory / ephemeral personal-plane state

These personal-plane runtime services keep state in-process only; none of this state is backed
by the durable ORM layer in Area 1.

- [`backend/communications/registry.py`](backend/communications/registry.py):
  `CommunicationsService` is an **in-process singleton** (obtained via
  `get_communications_service()`). It stores `Person`, `ContactIdentity`, `Communication`, and
  `Conversation` in memory. `Communication` rows carry only a `content_ref` (no full content).
  Module docstring (line 1) describes it as the "In-memory unified communications registry."
  No DB backing.
- [`backend/communications/search.py`](backend/communications/search.py): `text_resolver` is
  transient and not persisted.
- [`backend/communications/detection/service.py`](backend/communications/detection/service.py):
  `DetectionService` (line ~170–444, docstring "In-memory detection orchestrator") keeps
  in-memory `_reports`, `_attention_ids`, and `_communications`; `register_content` treats
  content as transient, and `detect()` stores detected communications in-process only.
- [`backend/communications/detection/wiring.py`](backend/communications/detection/wiring.py):
  `CommunicationPipeline` and `get_pipeline` provide in-process pipeline wiring.
- [`backend/agents/registry.py`](backend/agents/registry.py): the in-process `AgentRegistry`
  (line ~167–193) owns native agent types and manifests (`AgentCapabilities`, `AgentManifest`,
  `_create_native_manifests`). The registry definition itself is **not** persisted. Active agent
  runtime state is ephemeral: `AgentRun` (line ~314–379), `LoopDetector` (line ~382–426, spec §75),
  and blackboard-related state (`Blackboard`, `BlackboardEntry`, line ~97–164).

**Contrast note:** The durable `agents` table (Area 3) stores **created agent instances**, not
the in-process registry of agent *types*. Do not conflate the two.

---

## Area 3 — Durable personal-plane state

Durable personal-plane state is exactly the ORM model set in
[`backend/persistence/models/`](backend/persistence/models/) plus the Alembic-managed schema from
Area 1.

**Confirmed durable personal-plane model groups:**
- claims / decisions — `claims.py` (`claims`, `decisions`)
- tasks / task runs — `tasks.py` (`tasks`, `task_runs`)
- relationships — `relationships.py` (`relationships`)
- entities — `entities.py` (`entities`)
- evidence — `evidence.py` (`evidence`)
- goals / agent results — `goals.py` (`goals`, `agent_results`)
- agents / incidents — `incidents_agents.py` (`agents`, `incidents`)
- planned changes — `planned_changes.py` (`planned_changes`)
- events — `events.py` (`events`)
- auth — `auth.py` (`auth_sessions`, `auth_devices`, `pairing_challenges`, `approvals`)
- NerveCenter platform resources / principals / policies / approvals —
  `nc_platform.py` + migration `005_nc_platform.py`
  (`nc_resources`, `nc_connector_instances`, `nc_resource_aliases`, `nc_resource_relations`,
  `nc_resource_snapshots`, `nc_principals`, `nc_policies`, `nc_approvals`)

**Not durably stored by the audited persistence layer:** the Area 2 runtime services —
communications registry (persons / identities / communications / conversations), detection state
(`_reports`, `_attention_ids`, `_communications`), the transient text resolver, the agent
registry's type/manifest definitions, agent runs, blackboards, and loop-detection state.

---

## Area 4 — Endpoint behavior when durable storage is absent / unwired / unreachable

Endpoint adapters are built on the shared helpers in
[`backend/api/endpoints/_helpers.py`](backend/api/endpoints/_helpers.py).

### Read endpoints
- `read_collection(request, service_name, method_names)` (line ~16–54):
  - Resolves the service from `request.app.state.services`.
  - **Missing service → returns `[]`.**
  - Tries each configured method name in order; skips methods that are absent.
  - Awaits the method if it returns an awaitable.
  - `None` result → `[]`.
  - List result → serializes each item with `model_dump(mode="json")` when available.
  - Single-item result → wrapped in a list.
  - **Any exception from a service method → `[]`** (swallowed, not raised).
  - A code comment confirms the intent: read-only contract probes return a useful empty
    collection while dependencies are unavailable.
- `list_response(data)` (line ~57–67): stable `ListResponse` envelope with
  `version="1.0.0"`, `request_id`, an ISO timestamp, and pagination metadata.
- `make_read_router(prefix, tag, service_name, method_names)` (line ~70–100):
  - GET list routes for `""` and `"/"`.
  - GET `/{item_id}` scans the collection for a matching `id`.
  - **No match → a synthetic `{"id": item_id, "resource": tag}` at HTTP 200.**

**Net read behavior:** helper-based read endpoints **never return 5xx**. A missing service,
missing method, raised exception, `None`, or empty collection all yield **200 with an empty
list**; an unknown item id yields **200 with a synthetic representation**.

### Write endpoints
`make_write_endpoint(...)` (line ~134–256), inner `handler(request)` (line ~169–247):
- Resolves the service from `request.app.state.services` (as reads do).
- **No service configured → HTTP 202**, `accepted: true`, `executed: false`, `detail` explains
  no service was found, `result: null`.
- **Selected method raises → HTTP 202**, `executed: false`, `detail` includes the exception
  message, `result: null`.
- **No suitable method → HTTP 202**, `executed: false`, `result: null`.
- **Method succeeds → HTTP 200**, `accepted: true`, `executed: true`, `result` normalized via
  `_json_safe(...)` (line ~103–109).
- The raw JSON body is read manually; a malformed or absent body degrades to
  `arg_value = None` (no 4xx).

**Net write behavior:** write endpoints never surface a hard failure. Degrade paths all return
**202** with `executed: false`; only a real successful method call returns **200** with
`executed: true`.

### Startup / degradation wiring
[`backend/api/wiring.py`](backend/api/wiring.py) (docstring: "Startup wiring of durable
dependencies (DB engine, NATS client, services) onto app.state"):
- `resolve_db_url()` (line ~100–111) and `resolve_nats_url()` (line ~114–121) return `None` when
  the corresponding URL is unset — the engine/client are optional at startup.
- `build_db_engine()` (line ~152–172) — creates the async engine from the resolved URL; returns
  `None` when unset. `_build_fallback_engine(...)` (line ~124–149) is the fallback path.
- `build_services(engine)` (line ~193–215) — builds the service dict placed on
  `app.state.services`.
- `wire_dependencies(target)` (line ~218–267) — populates `target.state.db_engine`,
  `target.state.nats_client`, and `target.state.services`.
- `_build_and_connect_nats(target)` (line ~270–315) — builds the NATS client and runs a **bounded
  connect attempt**; failure does not abort startup.
- `teardown_dependencies(target)` (line ~318–341) — shuts down the NATS client and disposes the
  engine at shutdown.
- `install_dependency_wiring(target)` (line ~344–370) — registers startup/shutdown lifecycle via
  the ASGI lifespan (`wire_dependencies` on startup, `teardown_dependencies` on shutdown).

This wiring explains why the Area-4 endpoint behavior is safe when the database or NATS is
absent, unwired, or unreachable: the engine and client can legitimately be `None`, services can
be absent from `app.state.services`, and the helpers in `_helpers.py` degrade reads to empty
collections and writes to `202 executed: false` rather than raising.

---

## Area 5 — Future recovery / durability contract (requirements only)

The following are **non-implementation requirements** for a future recovery / durability
contract. This section deliberately contains **no** design, architecture, or implementation
detail.

1. **Durability boundary is the ORM layer.** The source of truth for personal-plane state that
   must survive process restart, crash, or redeploy is the durable ORM-backed persistence
   boundary (Area 1), not in-process service state (Area 2).
2. **Ephemeral state is recoverable or explicitly non-essential.** Each in-process personal-plane
   service's state (Area 2) must either be (a) reproducible from durable state, or (b) explicitly
   documented as non-essential to continuity. Neither case may silently assume that in-memory
   state survives across restarts.
3. **Write durability is verifiable, not assumed.** A write reported as accepted must have a
   defined, observable meaning for durability. An `executed: false` / `202` response (Area 4)
   must be distinguishable, by the client or an operator, from a durable commit — it must not be
   treated as evidence that data was written.
4. **Degradation must be detectable and reported.** When durable storage is absent, unwired, or
   unreachable, the system's health must be queryable and must reflect that degraded condition
   (cf. the `healthy` / `degraded` / `unhealthy` semantics of `check_health`, Area 1). A read that
   returns an empty list because a dependency is down (Area 4) must be distinguishable in
   observability from a read that returns an empty list because the collection is genuinely
   empty.
5. **Recovery preserves identity and semantics.** Any future recovery mechanism must preserve the
   stable identities and relationships of durable records (Area 3) so that recovered state is
   semantically equivalent to the pre-failure state, including cross-record references
   (e.g., relationships, evidence, decisions).
6. **Schema evolution is migration-gated.** Changes to the durable schema (Area 1) must be
   introduced only through the Alembic-managed migration chain, so that recovery can be reasoned
   about per-revision rather than against an undocumented live schema.
7. **Recovery must not conflate instance state with type definitions.** The durable `agents`
   table (created instances) and the in-process agent registry (type/manifest definitions) are
   distinct (Areas 2 and 3). A recovery contract must state which of these is in scope and must
   not treat the other as a substitute.
8. **Idempotency of recovery operations.** Any recovery procedure must be safe to re-run against
   already-recovered or already-applied state (consistent with the idempotent `CREATE EXTENSION
   IF NOT EXISTS vector` and idempotent-migration conventions already present in the durable
   boundary, Area 1).

---

## Verification notes / limitations
- **Verification method:** path inspection only. No tests, runtime probes, or migrations were run.
- **Table names:** all `__tablename__` values in the Area 1/3 inventory were read directly from
  the model files (no longer inferred). The only cross-check performed against a migration was
  `nc_connector_instances` in `005_nc_platform.py`; the remaining tables were verified at the
  ORM layer, not by opening each migration file individually.
- **Unverified:** exact column-level schema per table, index definitions, and any
  migration-specific constraints beyond what the migration docstrings state. These were not
  opened and are out of scope for this read-only audit.
- **No mutations performed** other than creating `specs/personal-ops-recovery/audit/` and this
  report file.
