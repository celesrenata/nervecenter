# Contract-Consistency Audit — Personal-Ops Recovery

**Transcribed** from the `m5-contract-audit` lane's inline result returned in the parallel batch
manifest.

| Field | Value |
| --- | --- |
| Batch id | `1ada822c-6ef8-426f-ae92-18a6294e7438` |
| Task name | `m5-contract-audit` |
| Worker | worker-5 (read-only, project-reader) |
| Date | 2026-10-27 |
| Method | Read-only contract-consistency audit. Worker-5 returned its findings inline in the batch `manifest.json` (`tasks[].result`) instead of writing a file. This report faithfully transcribes that inline result. |

---

## Findings

Worker-5's inline result is a numbered list of contract-consistency requirements. Each finding
below preserves the worker's wording and its cited line references verbatim. The `Lxxx`
references are worker-5's line citations into the source material it audited; the inline result
does not name the file(s) those lines refer to, so the exact file is recorded as `Unverified`
per finding.

### F1. Persistence ↔ backend contracts

The initial migration must create referenced tables before dependent tables, and the ORM
`Decision` must expose `planned_change_id`; fresh upgrade and downgrade-to-empty are acceptance
checks (L126–L127).

- Evidence: worker-5 inline result, finding 1; line refs L126–L127.
- Referenced file: `Unverified` (line citations only; file not named in the inline result).

### F2. Migration ↔ deployment contract

The deployment/integration path must invoke `python -m backend.persistence.migrate upgrade`, so
the migration entrypoint must remain callable under that module path (L274).

- Evidence: worker-5 inline result, finding 2; line ref L274.
- Referenced file: `Unverified` (line citations only).

### F3. Backend ↔ regression evidence

The non-integration backend suite must remain green; the excerpt records both a 9/9
post-integration gate (L138–L140) and a later full result of 1003 passed, 0 failed
(L206–L207).

- Evidence: worker-5 inline result, finding 3; line refs L138–L140, L206–L207.
- Referenced file: `Unverified` (line citations only).
- Note: the cited test counts (9/9; 1003 passed, 0 failed) are as reported by worker-5 and were
  not re-verified by this integration pass.

### F4. MCP ↔ backend route contract

The MCP surface must expose exactly 29 bound tools, including the 18 spec tools plus 11 added by
R112; the MCP assertions are the acceptance gate (L202, L208–L209).

- Evidence: worker-5 inline result, finding 4; line refs L202, L208–L209.
- Referenced file: `Unverified` (line citations only).

### F5. MCP/CLI ↔ API contract

`get_commitments` and the CLI commitments command must not claim completion while
`GET /api/commitments` is unshipped; this is explicitly tracked as a contract blocker
(L224–L226).

- Evidence: worker-5 inline result, finding 5; line refs L224–L226.
- Referenced file: `Unverified` (line citations only).

### F6. API ↔ web contract

API endpoint stubs must cover every specification §46 group before the web client can rely on
complete route coverage (L310).

- Evidence: worker-5 inline result, finding 6; line ref L310.
- Referenced file: `Unverified` (line citations only).

### F7. Web ↔ build verification

The web worker's integration check is `cd web && npm install && npm run build`; a successful
build is the stated web acceptance gate (L282).

- Evidence: worker-5 inline result, finding 7; line ref L282.
- Referenced file: `Unverified` (line citations only).

### F8. Cross-worker status contract

Workstream reporting must use only the declared statuses—`planned`, `in_progress`, `done`,
`blocked`, or `deferred`—so dependency and acceptance reporting remains machine-readable
(L5–L10).

- Evidence: worker-5 inline result, finding 8; line refs L5–L10.
- Referenced file: `Unverified` (line citations only).
