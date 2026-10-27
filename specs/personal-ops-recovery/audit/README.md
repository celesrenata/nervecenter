# Personal-Ops Recovery — Audit Index

Read-only audits for the P0 "Personal Operations Recovery" lift. All five reports below were
produced under batch `1ada822c-6ef8-426f-ae92-18a6294e7438` (date 2026-10-27). No source, config,
migration, endpoint, or deployment file was modified by any audit; the only writes in this
checkout are these report files under `specs/personal-ops-recovery/audit/`.

## Report files

| Report | Source | One-line verdict |
| --- | --- | --- |
| [persistence-infra.md](persistence-infra.md) | worker-3 (wrote file via patch) | Durable boundary is the ORM layer (Async SQLAlchemy + asyncpg + Alembic + Postgres/pgvector); in-process personal-plane state is ephemeral, and endpoint helpers silently degrade reads to empty 200 lists and writes to `202 executed:false` when durable storage is absent/unwired/unreachable. |
| [contracts-evidence.md](contracts-evidence.md) | worker-4 (wrote file via patch) | Static-only contracts-and-evidence audit: spec pack was absent at audit time (critical), NATS JetStream is `emptyDir`/ephemeral in-cluster, MCP Tool schema lacks a permission field, and write-plane endpoints 503 by design — plus a do-not-touch list of load-bearing contracts. |
| [backend-communications.md](backend-communications.md) | worker-1 (inline → transcribed) | Backend communications/detection planes are implemented end-to-end (ingest, attribution, detection, overdue sweep, pipeline wiring) but bodies live behind `content_ref`, attribution skips unresolvable peers, and the sweep excludes requests. |
| [web-apple.md](web-apple.md) | worker-2 (inline → transcribed) | Web has no Connection Center and no live mail-connector UI, Mac Relay install is manual, `/now` is not the default landing; iOS attention is usable, Watch approval is delegated to the iPhone, and Widgets are display-only with working stale indicators. |
| [contract-consistency.md](contract-consistency.md) | worker-5 / `m5-contract-audit` (inline → transcribed) | Eight contract-consistency requirements across persistence↔backend, migration↔deployment, backend↔regression, MCP↔backend, MCP/CLI↔API, API↔web, web↔build, and cross-worker status reporting. |

## Top findings across all audits

Deduplicated list drawn from the four substantive audit reports (each item cites its source
report; "Unverified" marks claims that could not be independently confirmed in this pass).

1. **Endpoint degradation is silent.** Read endpoints return `200` with an empty list and write
   endpoints return `202 executed:false` when durable storage is absent/unwired/unreachable —
   hard failures are never surfaced, so a degraded read is indistinguishable from an empty one.
   *(source: persistence-infra.md, Area 4)*
2. **In-process personal-plane state is not durable.** The communications registry
   (persons/identities/communications/conversations), detection state, and the agent registry
   live in memory and are not persisted by the ORM layer; the durability boundary is the ORM.
   *(source: persistence-infra.md, Areas 2–3)*
3. **NATS JetStream is ephemeral in-cluster.** The in-cluster deployment uses `emptyDir`
   (state lost on pod restart) while local compose persists it, yet the events API reads
   `/api/events` history from the bus — event history is durable only locally.
   *(source: contracts-evidence.md, F2 / §4.2)*
4. **MCP Tool schema has no permission field.** §45's Tool schema cannot express permission
   metadata, so any permissioned-tool contract is unexpressible without a schema change
   (tracked contract blocker). *(source: contracts-evidence.md, F3)*
5. **Write-plane endpoints 503 by design.** Goal/task create and incident create/transition
   are declared but undelivered; CLI/MCP surfaces that reference them exercise honest
   non-implementation. *(source: contracts-evidence.md, F4)*
6. **Spec pack was absent at audit time.** `specs/personal-ops-recovery/` did not exist in
   worker-4's workspace, so Section-1 spec markers were formally absent (critical); this
   integration is remediating it. *(source: contracts-evidence.md, F1)*
7. **Connection Center is Missing in the web.** There is no `/connections` route or
   navigation entry. *(source: web-apple.md, Gap list)*
8. **Guided Mac Relay install/pairing is Missing.** Relay setup remains manual: build, Full
   Disk Access, token or `relay.conf`, and LaunchAgent. *(source: web-apple.md, Gap list)*
9. **Web mail-connector live UI is Missing.** Settings renders documented instructions rather
   than a live connector flow. *(source: web-apple.md, Gap list)*
10. **Communication bodies are not embedded in detection artifacts.** Detection stores only
    transient text behind `content_ref`; finding events carry metadata only (content ref,
    status, confidence, due, why, match). *(source: backend-communications.md, Detection)*
11. **Overdue sweep excludes requests.** `sweep_overdue()` sets overdue commitments to
    `OVERDUE` and deadlines to `PASSING` but explicitly does not sweep requests — a scope
    decision the spec should resolve. *(source: backend-communications.md, Overdue sweep)*
12. **Person attribution skips unresolvable peers.** `_attribute_person()` returns no person
    unless identity resolution is `RESOLVED`, leaving such communications unattributed.
    *(source: backend-communications.md, Attribution)*
