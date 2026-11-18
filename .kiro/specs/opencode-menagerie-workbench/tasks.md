# Implementation Plan: opencode-menagerie-workbench

## Overview

The design is written in Python (FastAPI, SQLAlchemy, Alembic, hypothesis) and TypeScript (React, Vite, fast-check, Playwright), so no language question is needed. Work is split into three sequential delivery phases, each gated by a checkpoint and by verification evidence (Requirement 11.2 to 11.5):

- **Phase 1 (tasks 1 to 8):** storage and catalogue. NFS PVs and PVCs, file workers, project catalogue, health, audit, ADR-127, `/workbench` project views.
- **Phase 2 (tasks 9 to 14):** Code_Editor and opencode Sessions. File operations, Session proxy, usage records, the Monaco editor and the Session panel.
- **Phase 3 (tasks 15 to 20):** Browser_IDE. Kubernetes client, IDE manager and proxy, IDE image, RBAC, IDE NetworkPolicy, `/ide/` route and handoff link.

Phase routers are mounted only when `WORKBENCH_PHASE` is at least that phase. Each phase's property tests are written in the phase in which the code they check first ships. The PathGuard ships in Phase 1 because the file worker needs it for project creation, so Property 1 is tested in Phase 1 and is re-run at the Phase 2 gate.

## Tasks

- [x] 1. Phase 1: project foundations
  - [x] 1.1 Add dev dependencies and create the Workbench package skeleton
    - Add `hypothesis` to the dev dependencies in `pyproject.toml` and `fast-check` to `web/package.json` devDependencies
    - Create `backend/workbench/__init__.py` and `backend/workbench/phase.py` that reads `WORKBENCH_PHASE` (0 to 3, default 0), `WORKBENCH_IDE_ENABLED`, `WORKBENCH_UID` and `WORKBENCH_GID` (default 1000), `WORKBENCH_DEFAULT_MODEL` and `WORKBENCH_DEFAULT_SMALL_MODEL`
    - Create the test directories `tests/backend/workbench/` and `web/src/components/workbench/__tests__/`
    - _Requirements: 11.2, 11.3_

  - [x] 1.2 Add migration `049_workbench` and SQLAlchemy models
    - Create `backend/persistence/alembic/versions/049_workbench.py` with `down_revision = "048_kube_run_cap"`, creating only `workbench_projects`, `workbench_sessions`, `workbench_model_usage`, `workbench_ide` and `workbench_audit` with the columns, CHECK constraints and `UNIQUE (slug, kind)` from the design
    - Write a `downgrade()` that drops only the tables this revision created
    - Add the models under `backend/persistence/models/`
    - The usage table has no prompt or completion text columns
    - _Requirements: 2.14, 8.8, 6.11_

  - [x] 1.3 Write migration test
    - Upgrade and downgrade `049_workbench` on a scratch database
    - Scan the revision's operations and assert there is no drop or alter of existing tables or columns
    - _Requirements: 2.14_

- [x] 2. Phase 1: path guard and validation (pure logic)
  - [x] 2.1 Implement `PathGuard` in `backend/workbench/paths.py`
    - `PathGuard.resolve(root, rel)` returns a resolved path or raises `PathNotPermitted`
    - Reject paths that are over 4096 characters, contain a NUL, are absolute, contain a `..` segment (split on `/` and `\`), or contain percent-encoded separators or dot segments after one decode
    - Check containment with `os.path.realpath` and `os.path.commonpath`, so symlinks that point outside the root are rejected
    - For leaf creation, resolve the parent and then append the validated final component
    - Provide a helper for opening the leaf with `O_NOFOLLOW`
    - _Requirements: 4.12, 7.1, 7.2, 7.3_

  - [x] 2.2 Write property test for Path_Guard containment
    - **Property 1: Path_Guard containment**
    - Use a custom strategy mixing random text, `..`, `%2e%2e`, NUL, absolute prefixes and a temp directory with symlinks pointing inside and outside the root
    - Assert that resolve either returns a path inside the root or raises, and that no file changes when it raises
    - **Validates: Requirements 4.12, 7.1, 7.2, 7.3, 11.6**

  - [x] 2.3 Implement Phase 1 validators in `backend/workbench/validation.py`
    - `validate_slug` (`^[a-z0-9][a-z0-9-]{0,62}$`), `validate_title` (1 to 120 characters), `validate_kind`, `validate_git_url` (only `https` or `ssh` schemes), and `low_space(free_bytes)` (below 5,368,709,120)
    - Define the Pydantic request models with `extra="forbid"`: `ProjectIn` and the delete confirmation body
    - _Requirements: 2.3, 2.4, 2.7, 9.5_

  - [x] 2.4 Write property test for input validation
    - **Property 2: Input validation rejects without side effects**
    - Generate slugs, titles, kinds and git URLs, and assert that creation succeeds only for valid input and that invalid input creates no Project_Root and no record
    - **Validates: Requirements 2.3, 2.4, 2.7, 11.6**

  - [x] 2.5 Write property test for the low-space threshold
    - **Property 24: Low-space warning threshold**
    - **Validates: Requirements 9.5**

  - [x] 2.6 Write unit tests required by Requirement 11.6 for paths and slugs
    - Path_Guard rejects `..`, absolute, symlink-escape and NUL paths
    - Slug validation accepts valid slugs and rejects empty, over-length and disallowed-character slugs
    - _Requirements: 11.6, 7.2, 2.3_

- [x] 3. Phase 1: storage layer and catalogue service
  - [x] 3.1 Implement the file worker Phase 1 operations in `backend/workbench/fsworker.py`
    - FastAPI app run as `python -m backend.workbench.fsworker --kind <kind>`
    - Endpoints `POST /mkproject` (write to `.tmp-<id>` then rename, so no partial Project_Root), `POST /clone` (300 s limit, abort and remove the partial directory on failure or timeout), `POST /rmproject`, `GET /dirs`, `GET /stat`, `GET /probe` (mounted, writable, free bytes within 5 s)
    - Every path-taking endpoint calls `PathGuard.resolve` first
    - _Requirements: 1.5, 1.6, 1.7, 1.8, 2.2, 2.6, 2.8, 2.10, 2.12, 9.3, 9.4_

  - [x] 3.2 Implement `StoreClient` in `backend/workbench/store.py`
    - Call the worker for a kind with a 10 s timeout and map failures to `StoreUnavailable(kind)`
    - The error names the unavailable store, and the other store keeps serving
    - _Requirements: 1.8_

  - [x] 3.3 Implement `AuditLog` in `backend/workbench/audit.py`
    - `record(actor, project, action, outcome)` writes a `workbench_audit` row and one structured log line
    - Cap the serialized entry at 4 KiB, and accept no payload text, so entries contain no file contents, prompts or secrets
    - _Requirements: 6.11, 6.12_

  - [x] 3.4 Write property test for audit entries
    - **Property 19: Audit entries**
    - Use arbitrary payload contents and assert exactly one entry with actor, slug, action and outcome, at most 4 KiB, and none of the payload text
    - **Validates: Requirements 6.11, 6.12**

  - [x] 3.5 Implement store health in `backend/workbench/health.py`
    - Probe each store and return `mounted`, `writable` and `free_bytes`
    - On a probe timeout of 5 s report `mounted: false`, `writable: false`, `free_bytes: null` and still return the other store
    - _Requirements: 9.3, 9.4_

  - [x] 3.6 Implement `ProjectService` in `backend/workbench/projects.py`
    - Create (empty or from a git URL), list (archived included, ordered by last activity descending), get (with sessions and the 10 most recent handoffs from a read-only handoff query, newest first), archive, delete with confirmation equal to the slug, discover unregistered directories, import, set push approval
    - Conflict on duplicate `(slug, kind)`
    - Archive, list-unregistered and import never modify files
    - Never touch `coding_handoffs` except the read-only query
    - _Requirements: 1.6, 1.7, 2.1, 2.2, 2.5, 2.6, 2.9, 2.10, 2.11, 2.12, 2.13, 3.5, 7.13, 10.2_

  - [x] 3.7 Write property test for create-then-read round trip and store placement
    - **Property 3: Project create then read round trip and store placement**
    - Include the named Requirement 11.6 test that a Project created, saved and read back returns identical content
    - **Validates: Requirements 1.6, 1.7, 2.2, 2.15, 11.6**

  - [x] 3.8 Write property test for lifecycle operations preserving files
    - **Property 4: Lifecycle operations preserve files**
    - **Validates: Requirements 2.9, 2.10, 2.11, 2.12, 2.13**

  - [x] 3.9 Write property test for project list ordering
    - **Property 5: Project list ordering and grouping** (backend ordering and archived inclusion)
    - **Validates: Requirements 2.1, 3.3, 3.4**

  - [x] 3.10 Write failure-injection tests for the storage layer
    - A store timeout leaves the other store usable and creates no partial root
    - A clone failure and a clone timeout remove the partial directory and record nothing
    - A health probe timeout reports the store as unmounted
    - _Requirements: 1.8, 2.8, 9.4_

- [x] 4. Phase 1: Workbench API router
  - [x] 4.1 Create `backend/api/endpoints/workbench.py` and register it in `backend/api/app.py`
    - Prefix `/api/workbench`, with every route depending on the existing SSO session dependency
    - Phase 1 routes: `GET /config`, `GET /health`, `GET /projects`, `POST /projects`, `GET /projects/{kind}/{slug}`, `POST /projects/{kind}/{slug}/archive`, `DELETE /projects/{kind}/{slug}`, `GET /unregistered`, `POST /projects/import`, `POST /projects/{kind}/{slug}/push-approval`
    - Require the `X-NC-Workbench: 1` header on every `POST`, `PUT`, `PATCH` and `DELETE`, and return 403 for a cross-origin request without it
    - Mount routers by `WORKBENCH_PHASE`, so higher-phase routes are not mounted
    - Write one audit entry for create, delete and archive, on success and on failure
    - Map errors per the design: 409 `project_exists`, 422 validation, 503 `store_unavailable`, 502 `clone_failed` and `clone_timeout`
    - _Requirements: 2.1 to 2.13, 3.1, 3.2, 6.1, 6.4, 6.9, 6.10, 6.11, 9.3, 11.3_

  - [x] 4.2 Write API tests for authentication and gating
    - Route-enumeration test: every route on the Workbench router returns 401 without a session and performs no operation
    - Named Requirement 11.6 test: an unauthenticated request returns no Project data and changes nothing
    - A phase 2 or phase 3 route is not mounted while `WORKBENCH_PHASE` is 1
    - A state-changing request without the CSRF header returns 403, and a wrong method on a state-changing path returns 405
    - _Requirements: 6.1, 6.4, 6.9, 6.10, 11.3, 11.6_

- [x] 5. Phase 1: manifests, ADR and operations docs
  - [x] 5.1 Create `deployments/kubernetes/optional/workbench/` base manifests
    - `namespace.yaml` (`workbench`, with `pod-security.kubernetes.io/enforce: restricted`)
    - `pv-pvc-opencode.yaml` and `pv-pvc-menagerie.yaml` with `Retain`, `ReadWriteMany`, mount options `nfsvers=4.1,hard` and the two NFS paths on `192.168.42.8`
    - `kustomization.yaml`, not referenced from the base kustomization
    - No `emptyDir` fallback for project data
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 9.1, 9.10, 9.11_

  - [x] 5.2 Create file worker and NetworkPolicy manifests
    - `fsworker.yaml` with Deployments `wb-fs-opencode` and `wb-fs-menagerie`, each running `python -m backend.workbench.fsworker --kind <kind>`, mounting exactly one store, running as `WORKBENCH_UID` and `WORKBENCH_GID`, with non-zero resource requests and limits
    - `networkpolicy.yaml` with default-deny ingress and egress in `workbench`, worker ingress only from API pods, and worker egress to DNS and public TCP 443 and 22 (RFC 1918, `169.254.0.0/16` excluded)
    - Add both files to `kustomization.yaml`
    - _Requirements: 1.5, 7.7, 7.8, 7.9, 7.10, 9.1_

  - [x] 5.3 Create `nfs-check.sh`
    - For each export, list it, write a marker, read it back with identical content, check ownership equals `WORKBENCH_UID` and `WORKBENCH_GID`, and remove it
    - Exit 0 on success and non-zero naming the failing export on any failure
    - Support a path override through environment variables for testing
    - _Requirements: 1.10_

  - [x] 5.4 Write manifest and script tests
    - `kustomize build` renders and both PVs have `Retain`, `ReadWriteMany` and `nfsvers=4.1,hard`
    - No Pod template mounts both stores, the base kustomization does not reference the folder, and no new Ingress path exists
    - `nfs-check.sh` exits 0 on a temp directory and non-zero naming the export when a path is unwritable
    - _Requirements: 1.3, 1.4, 1.5, 1.10, 6.3, 9.1, 9.11_

  - [x] 5.5 Append ADR NS-ADR-127 to `.kiro/specs/nerve-shield/decisions.md`
    - Status PROPOSED
    - Three separate sections: (a) extension of NS-ADR-106 to in-cluster interactive code execution, (b) storage design, (c) isolation model
    - _Requirements: 11.1_

  - [x] 5.6 Write ADR test
    - Assert the identifier is unique and the three sections exist
    - _Requirements: 11.1_

  - [x] 5.7 Write `docs/workbench/README.md`
    - Follow the style of `docs/central-opencode/README.md`
    - Numbered enable procedure where each step names the command and the confirming check, and a numbered rollback that deletes nothing on the exports
    - Document the NFS mount options and their rationale, the UID and GID requirement, the NFS attribute-cache limit, that backups of both exports are the Owner's responsibility, and the restore check
    - Document that the Longhorn PVC `opencode-data` is unchanged and that a pod stays Pending when the NFS server is unreachable
    - _Requirements: 1.9, 9.2, 9.9, 9.10, 9.11_

- [x] 6. Phase 1: web views
  - [x] 6.1 Create `web/src/api/workbench.ts`
    - Axios client following `handoffs.ts`, adding the `X-NC-Workbench: 1` header on state-changing calls
    - Phase 1 calls: config, health, project list, create, get, archive, delete, unregistered, import, push-approval
    - _Requirements: 3.13, 6.10_

  - [x] 6.2 Create `web/src/views/WorkbenchView.tsx` and a pure `groupProjects` helper
    - Projects grouped by Workspace_Kind, each group labeled with its kind and project count
    - Archived projects hidden by default, with a filter that includes them marked archived and keeps its state until the Owner leaves `/workbench`
    - Low-space banner naming the store and free space, removed within 60 s of recovery
    - Create dialog and unregistered-directory import
    - Errors use `role="alert"`
    - _Requirements: 2.12, 3.3, 3.4, 3.13, 9.5_

  - [x] 6.3 Create `web/src/views/WorkbenchProjectView.tsx`
    - Show the project, the 10 most recent handoffs newest first, and the archive and delete controls (delete requires typing the slug)
    - _Requirements: 2.9, 2.10, 2.11, 3.5, 3.13_

  - [x] 6.4 Wire routes and navigation
    - Add `/workbench` and `/workbench/:kind/:slug` routes in `web/src/App.tsx`
    - Add the Workbench nav entry in `web/src/components/Layout.tsx`, shown when `config.phase >= 1`
    - _Requirements: 3.1, 3.2_

  - [x] 6.5 Write property test for project grouping
    - **Property 5: Project list ordering and grouping** (grouping and archived visibility, using `fast-check`)
    - **Validates: Requirements 2.1, 3.3, 3.4**

  - [x] 6.6 Create `web/tests/e2e/workbench.spec.ts` with Phase 1 cases
    - Archived filter behaviour and the low-space banner appearing and clearing
    - Use the repository's existing `global-setup.mjs` and helpers
    - _Requirements: 3.4, 9.5_

- [x] 7. Phase gate evidence tooling
  - [x] 7.1 Create `scripts/workbench_phase_evidence.py`
    - Take a phase number, run that phase's automated tests, and write `docs/workbench/evidence-phase-N.md` with test count, pass count, fail count and the run date
    - Exit non-zero and mark the phase incomplete when any test fails
    - _Requirements: 11.4, 11.5_

- [x] 8. Checkpoint - Phase 1 gate
  - Ensure all tests pass, ask the user if questions arise.
  - Run `nfs-check.sh` from a node and generate `docs/workbench/evidence-phase-1.md`. Phase 2 stays disabled until the Owner reviews the evidence and raises `WORKBENCH_PHASE`.

- [x] 9. Phase 2: pure logic for files, sessions and policy
  - [x] 9.1 Extend `backend/workbench/validation.py`
    - `classify_file` (openable only when size is at most 2,097,152 bytes and valid UTF-8, with the exact size and detected type reported otherwise)
    - `language_for(path)` (`plaintext` for unknown extensions), `sort_tree(entries)` (directories first, then names ascending)
    - `validate_message` (1 to 20,000 characters, Session running), `validate_selection_action` (selection at most 100,000, instruction 1 to 10,000, Session active)
    - `is_local_route` (wraps `backend.inference.local_first.is_local`), `needs_nonlocal_confirm`
    - `classify_git_command(argv, push_approved)` (deny `push` and remote add, set-url, rename and remove unless approved)
    - _Requirements: 3.8, 3.9, 4.1, 4.2, 4.6, 4.10, 4.11, 7.13, 8.5_

  - [x] 9.2 Write property test for file classification
    - **Property 9: File classification**
    - **Validates: Requirements 4.6**

  - [x] 9.3 Write property test for tree ordering and language selection
    - **Property 10: File tree ordering and language selection**
    - **Validates: Requirements 4.1, 4.2**

  - [x] 9.4 Write property test for message and selection limits
    - **Property 12: Message and selection limits**
    - **Validates: Requirements 3.8, 3.9, 4.10, 4.11**

  - [x] 9.5 Write property test for the git push policy
    - **Property 21: Git push policy**
    - **Validates: Requirements 7.13**

- [x] 10. Phase 2: file operations
  - [x] 10.1 Extend `backend/workbench/fsworker.py` with file operations
    - `GET /list`, `GET /read`, `PUT /write`, `POST /op` (create, rename, move, delete), and a changes-since query for external edits
    - Atomic write: temp file in the same directory, `fsync`, compare `st_mtime_ns` with `base_mtime_ns` under a per-file `flock`, `os.replace`, `fsync` the directory, and return the new mtime
    - A stale `base_mtime_ns` returns a conflict carrying the current mtime and leaves the file unchanged
    - A per-project semaphore of 4 for writes, with additional writes rejected with the concurrency-limit error
    - Refuse files over 2 MiB or not UTF-8, reporting size and type
    - _Requirements: 4.1, 4.3, 4.4, 4.5, 4.6, 4.8, 4.12, 9.12_

  - [x] 10.2 Implement `backend/workbench/files.py`
    - API-side facade that forwards file operations to the worker for the project's kind
    - Resolve `(slug, "opencode")` and return 409 `editor_not_supported` for a `menagerie`-only slug
    - Map errors: 400 `path_not_permitted`, 409 `conflict`, 415 `not_editable`, 429 `concurrency_limit`
    - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.12, 9.12_

  - [x] 10.3 Write property test for the save and read round trip
    - **Property 6: File save and read round trip**
    - Include mixed `\n` and `\r\n` line endings and trailing newline state, up to 2 MiB
    - Include the named Requirement 11.6 round-trip unit test
    - **Validates: Requirements 4.14, 11.6**

  - [x] 10.4 Write property test for stale saves
    - **Property 7: Stale saves are rejected**
    - **Validates: Requirements 4.5**

  - [x] 10.5 Write property test for file operations against a model filesystem
    - **Property 8: File operations match a model filesystem**
    - Use `hypothesis.stateful.RuleBasedStateMachine` over create, rename, move and delete
    - **Validates: Requirements 4.4**

  - [x] 10.6 Write property test for the write concurrency cap
    - **Property 26: Write concurrency cap**
    - **Validates: Requirements 9.12**

- [x] 11. Phase 2: opencode Sessions
  - [x] 11.1 Extend `OpenCodeClient` in `backend/handoffs/central.py`
    - Add session create with working directory and models, send message, abort, list and an event stream
    - Confirm event names and the abort endpoint against the pinned opencode image `2.3.0`
    - Existing handoff behaviour must stay identical
    - _Requirements: 3.6, 3.8, 3.10, 10.1, 10.6_

  - [x] 11.2 Implement `backend/workbench/sessions.py`
    - Create a Session with `location=/projects/<slug>`, default models `omniroute/local/code` and `local/fast`, and `session_permissions(push_approved)`
    - Return 409 `nonlocal_confirm_required` when the model is not local and `confirm_nonlocal` is absent
    - Relay SSE events filtered to the Session in received order, emitting `file_changed` events for edits
    - Use a 5 s timeout and return 503 `opencode_unavailable`, and a 10 s wait for abort confirmation that otherwise returns 504 and keeps the Session running
    - Never create, modify or delete a handoff row
    - _Requirements: 3.6, 3.7, 3.8, 3.10, 3.12, 4.8, 8.1, 8.3, 8.5, 8.6, 10.2_

  - [x] 11.3 Implement `backend/workbench/usage.py`
    - Record Session id, model, input tokens, output tokens and timestamp within 10 s
    - Never store prompt or completion text
    - _Requirements: 8.8_

  - [x] 11.4 Add Phase 2 routes to `backend/api/endpoints/workbench.py`
    - Files: `GET /projects/{slug}/files`, `GET` and `PUT /projects/{slug}/file`, `POST /projects/{slug}/fs`, `GET /projects/{slug}/changes`
    - Sessions: `POST` and `GET /projects/{slug}/sessions`, `GET /sessions/{id}/events`, `POST /sessions/{id}/messages`, `POST /sessions/{id}/abort`, `POST /sessions/{id}/selection-action`
    - A usage listing endpoint for the model-policy panel
    - Audit entries for file write and Session start
    - Never return the Opencode_Server password in any body, header or log
    - _Requirements: 3.6 to 3.10, 4.1 to 4.12, 6.5, 6.11, 8.8, 8.9_

  - [x] 11.5 Write property test for stream order preservation
    - **Property 13: Stream order preservation**
    - **Validates: Requirements 3.7**

  - [x] 11.6 Write property test for model locality and confirmation
    - **Property 22: Model locality and confirmation**
    - Assert the generated opencode configuration lists OmniRoute as its only provider and contains no API key
    - **Validates: Requirements 8.1, 8.5, 8.6**

  - [x] 11.7 Write property test for content-free usage records
    - **Property 23: Usage records are content-free**
    - **Validates: Requirements 8.8**

  - [x] 11.8 Write property test for secret non-disclosure
    - **Property 20: Secrets are never disclosed**
    - Use random password values, and add a fixed-value regression that searches every endpoint's responses and logs
    - **Validates: Requirements 6.5**

  - [x] 11.9 Write handoff regression and Session failure tests
    - Count `coding_handoffs` rows and contents before and after a Session start, and assert they are identical
    - Confirm the existing `/api/handoffs/*` tests and `tests/apple` HandoffAgent tests are unchanged and still pass
    - Opencode timeout returns the unavailable state, and an unconfirmed abort within 10 s leaves the Session running
    - _Requirements: 3.10, 3.12, 10.1, 10.2, 10.6_

- [x] 12. Phase 2: manifests and docs
  - [x] 12.1 Add the `opencode` namespace claim manifest
    - Create `pv-pvc-opencode-ns.yaml` (`oc-opencode-projects`) for the Opencode_Store and add it to `kustomization.yaml`
    - Do not modify the `opencode-data` PVC
    - _Requirements: 1.4, 1.5, 1.9_

  - [x] 12.2 Document the opencode `/projects` mount
    - Add a note in `docs/central-opencode` for the NFS `/projects` mount on the opencode Deployment (the Session working directory is `/projects/<slug>`)
    - Extend the enable and rollback procedures in `docs/workbench/README.md` for Phase 2
    - _Requirements: 1.9, 3.6, 9.2_

  - [x] 12.3 Extend manifest tests for Phase 2
    - The `opencode-data` PVC manifest is byte-identical to the mirror, and no Pod template mounts both stores
    - _Requirements: 1.5, 1.9_

- [x] 13. Phase 2: editor and Session web UI
  - [x] 13.1 Add the editor dependency and bundle configuration
    - Add `monaco-editor` and `@monaco-editor/react`, with `?worker` imports in the Vite config and `loader.config({ monaco })`, so nothing loads from a CDN
    - _Requirements: 4.13_

  - [x] 13.2 Extend `web/src/api/workbench.ts`
    - Add file, filesystem-operation, changes, Session, message, abort and selection-action calls, and an SSE helper modeled on `askStream.ts`
    - _Requirements: 3.7, 4.1, 4.3, 4.4_

  - [x] 13.3 Implement `web/src/components/workbench/editorState.ts`
    - Pure reducer for tabs, dirty flags, external-change notices and reload
    - An external-change notice never alters a dirty buffer, and the buffer changes only on explicit reload or successful save
    - _Requirements: 4.7, 4.8, 4.9_

  - [x] 13.4 Write property test for unsaved edits
    - **Property 11: Unsaved edits are never clobbered**
    - Use `fast-check` with at least 100 runs
    - **Validates: Requirements 4.8, 4.9**

  - [x] 13.5 Implement `web/src/components/workbench/CodeEditor.tsx`
    - The only module that imports Monaco, lazy-loaded
    - Syntax highlighting from the file extension, with plain text for unknown extensions
    - _Requirements: 4.2, 4.13_

  - [x] 13.6 Implement `web/src/components/workbench/SessionPanel.tsx`
    - Streamed messages and tool-call summaries, message box, abort, and the model selector
    - Show a "non-local" label and require confirmation for non-local models, keeping the previous selection when declined
    - Show the "opencode unavailable" state without breaking the project list or editor link
    - Reject invalid messages with a `role="alert"` error and keep the entered text
    - _Requirements: 3.6 to 3.10, 3.12, 8.5, 8.6_

  - [x] 13.7 Implement `web/src/views/WorkbenchEditorView.tsx`
    - File tree (10,000 entries in 2 s) and tabs, with an unsaved indicator on the tab and in the tree, removed within 500 ms of a successful save
    - Save conflict handling that keeps the Owner's edits
    - A `beforeunload` prompt while any tab is dirty
    - A notice naming a file changed by a Session with a reload action
    - Selection-to-opencode action with limit checks and a displayed error when no Session is active
    - Hide the editor entry for `menagerie` Projects
    - _Requirements: 4.1 to 4.11_

  - [x] 13.8 Wire the editor and Session routes
    - Add `/workbench/:slug/edit` in `web/src/App.tsx`
    - Add the Session list and the editor link to `WorkbenchProjectView.tsx`
    - _Requirements: 3.5, 3.6_

  - [x] 13.9 Show Session usage in the existing model-policy panel
    - Locate the existing panel view and display each Session's recorded model name and token counts alongside its existing entries
    - _Requirements: 8.9_

  - [x] 13.10 Extend `web/tests/e2e/workbench.spec.ts` with Phase 2 cases
    - Open the Code_Editor, edit a file, save, reload, and find the saved content unchanged
    - Unsaved indicator and `beforeunload` prompt, same-origin-only request check, and Session usage in the model-policy panel
    - _Requirements: 4.7, 4.13, 8.9, 11.7_

- [x] 14. Checkpoint - Phase 2 gate
  - Ensure all tests pass, ask the user if questions arise.
  - Generate `docs/workbench/evidence-phase-2.md`. Phase 3 stays disabled until the Owner reviews the evidence and raises `WORKBENCH_PHASE`.

- [x] 15. Phase 3: Kubernetes client and IDE manager
  - [x] 15.1 Implement `backend/workbench/kubeclient.py`
    - Minimal `httpx` client for the Kubernetes REST API using the in-cluster ServiceAccount token
    - Only the verbs the manager needs: pods get, list, create and delete, secrets get, create and delete, events list
    - _Requirements: 5.3, 5.8, 5.11_

  - [x] 15.2 Implement the pure parts of `backend/workbench/ide.py`
    - `build_pod_spec(project, config, key_secret_name)` producing namespace `workbench`, a single `subPath` mount of the project, the full security context, non-zero requests and limits, `automountServiceAccountToken: false`, `ROO_CODE_IPC_SOCKET_PATH`, `--auth none`, `WORKBENCH_PUSH_APPROVED`, the per-IDE OmniRoute key by `secretKeyRef`, and an image pinned by digest
    - `resolve_upstream(slug, instances)`, `select_idle(instances, now)` (idle at least 60 minutes), and `emit_event` (one structured log entry and one counter increment per event, with no file contents or names)
    - _Requirements: 5.3 to 5.6, 5.7, 5.9, 6.6, 7.4, 7.5, 7.6, 7.11, 7.12, 8.7, 9.6_

  - [x] 15.3 Implement `IdeManager` in `backend/workbench/ide.py`
    - Start: enforce the maximum of 2 running, return the existing pod for a project that has one, issue the per-IDE OmniRoute key through `omniroute_keys.py` machinery into a Secret owned by the pod, and wait up to 120 s for Ready
    - On timeout, delete the pod first and then the Secret, and report the last pod event message
    - Stop within 30 s, list, `rediscover()` by label `app=ide` within 30 s of startup, and the 15 s idle reaper using `last_traffic_at` in `workbench_ide`
    - Return 409 `ambiguous_slug` when a slug exists in both kinds
    - _Requirements: 5.3, 5.8 to 5.11, 5.13, 7.12, 9.7, 9.8_

  - [x] 15.4 Implement the IDE proxy in `backend/workbench/ide_proxy.py`
    - Serve `/ide/<slug>/` for HTTP and WebSocket and forward to the project's pod IP only
    - For WebSocket upgrades, validate the SSO session and `Origin` before connecting upstream: 401 for no session, 403 for a missing or non-matching `Origin`, and no upstream connection on refusal
    - Update `last_traffic_at` at most once per 30 s per pod
    - _Requirements: 5.7, 5.9, 6.2, 6.7, 6.8_

  - [x] 15.5 Write property test for IDE pod spec invariants
    - **Property 14: IDE pod spec invariants**
    - **Validates: Requirements 5.3, 5.4, 5.5, 5.6, 6.6, 7.4, 7.5, 7.6, 7.11, 7.12, 8.7**

  - [x] 15.6 Write property test for IDE registry limits
    - **Property 15: IDE registry limits**
    - Use a `RuleBasedStateMachine` over a fake cluster client
    - **Validates: Requirements 5.10, 5.13, 9.7**

  - [x] 15.7 Write property test for the idle reaper decision
    - **Property 16: Idle reaper decision**
    - **Validates: Requirements 5.9**

  - [x] 15.8 Write property test for IDE routing isolation
    - **Property 17: IDE routing isolation**
    - **Validates: Requirements 5.7**

  - [x] 15.9 Write property test for request gates
    - **Property 18: Request gates (WebSocket Origin, session, method, CSRF)**
    - **Validates: Requirements 6.7, 6.8, 6.9, 6.10**

  - [x] 15.10 Write property test for IDE lifecycle events
    - **Property 25: IDE lifecycle events are logged and counted once**
    - **Validates: Requirements 9.6**

  - [x] 15.11 Write IDE failure-injection tests
    - A start that is not ready in 120 s deletes the pod and Secret and returns the last event message
    - A third start is rejected without creating a pod
    - Stop leaves project files unchanged
    - _Requirements: 5.8, 5.10, 5.11_

- [x] 16. Phase 3: IDE API routes
  - [x] 16.1 Add Phase 3 routes to `backend/api/endpoints/workbench.py` and the proxy routes to `backend/api/app.py`
    - `GET /api/workbench/ide`, `POST /ide/{kind}/{slug}/start`, `POST /ide/{kind}/{slug}/stop`
    - A Menagerie task-status endpoint that polls the IDE image's `menagerie-status` endpoint
    - `ide_enabled` in `GET /config`
    - Audit entries for IDE start and stop, and IDE event metrics through `backend/observability`
    - _Requirements: 3.11, 5.3, 5.7, 5.8, 5.12, 6.11, 9.6_

  - [x] 16.2 Write property test for handoff selection
    - **Property 27: Handoff selection and the Browser IDE link** (the 10 newest in newest-first order, in Python)
    - **Validates: Requirements 3.5, 10.3, 10.4**

- [x] 17. Phase 3: IDE container image
  - [x] 17.1 Create the IDE image build
    - Dockerfile with code-server and the Menagerie extension pinned to exact versions, with the VSIX supplied by build args `MENAGERIE_VSIX` and `MENAGERIE_VERSION` and a failing build when either is missing
    - Install and activate the extension, and put a `git` wrapper first on `PATH` that applies the push policy
    - Add the small `menagerie-status` endpoint on a second port
    - Embed no provider API key, and mount no git credentials
    - _Requirements: 5.1, 5.2, 7.13, 8.4_

  - [x] 17.2 Write image build-file tests
    - code-server and the extension are pinned to exact versions, the build fails without the VSIX args, and no provider API key pattern appears in the build context
    - _Requirements: 5.2, 8.4_

- [x] 18. Phase 3: manifests and docs
  - [x] 18.1 Add RBAC and API patch manifests
    - `rbac.yaml` with ServiceAccount `nervecenter-workbench`, and a Role in `workbench` allowing pods get, list, watch, create and delete, secrets get, create and delete, and events list
    - `api-patch.yaml` setting the API Deployment's ServiceAccount, and no ClusterRole
    - Add both to `kustomization.yaml`
    - _Requirements: 5.3, 7.12, 9.1_

  - [x] 18.2 Add IDE NetworkPolicy rules and a probe script
    - In `networkpolicy.yaml`: IDE ingress only from API pods on the code-server port, and egress only to DNS, OmniRoute on TCP 20128 (copy the selector from `docs/central-opencode/networkpolicy.yaml`), and public TCP 443 and 22 with RFC 1918 and `169.254.0.0/16` excluded
    - Add a probe script that runs a throwaway pod in `workbench` and checks the database, API port, Kubernetes API server, `169.254.169.254` and an RFC 1918 address all fail and DNS, OmniRoute and a public 443 host succeed
    - _Requirements: 7.7, 7.8, 7.9, 7.10_

  - [x] 18.3 Add the `/ide/` location to `web/nginx.conf.template`
    - Proxy to the API with `proxy_http_version 1.1`, `Upgrade` and `Connection` headers, long read timeouts, and the `Origin` header preserved
    - Add no new Ingress path
    - _Requirements: 6.2, 6.3, 5.7_

  - [x] 18.4 Extend `docs/workbench/README.md` for Phase 3
    - Enable and rollback steps for the Browser_IDE, the VSIX build arguments, the git wrapper limit (a guard rail, with the hard limit being that no git credentials are mounted), and the Menagerie status assumption
    - _Requirements: 9.2, 7.13, 3.11_

  - [x] 18.5 Extend manifest tests for Phase 3
    - Role has no ClusterRole, no Pod template mounts both stores, and no new Ingress path exists
    - _Requirements: 1.5, 6.3, 7.12_

- [x] 19. Phase 3: IDE web UI and handoff link
  - [x] 19.1 Extend `web/src/api/workbench.ts` with IDE calls
    - List, start and stop IDE instances and the Menagerie task status
    - _Requirements: 5.3, 5.8, 3.11_

  - [x] 19.2 Implement `web/src/components/workbench/IdeFrame.tsx`
    - Start and stop controls and the `/ide/<slug>/` iframe once ready
    - Hide all Browser_IDE controls when `ide_enabled` is false, with the editor and Sessions unaffected
    - Show the limit-reached and startup-failure errors with `role="alert"`
    - _Requirements: 5.7, 5.10, 5.11, 5.12_

  - [x] 19.3 Add the "Open in Browser IDE" link to the Handoffs view
    - A pure predicate in `web/src/components/workbench/handoffLink.ts`: show the link only when the handoff targets `menagerie` and a `menagerie` Project exists whose slug equals the handoff's repo slug
    - Edit `HandoffsView` to render a plain anchor that opens the Project's Workbench view and changes no handoff field
    - Leave every other Handoffs behaviour unchanged
    - _Requirements: 10.1, 10.3, 10.4, 10.5_

  - [x] 19.4 Show the IDE controls and Menagerie task status in `WorkbenchProjectView.tsx`
    - Embed `IdeFrame`, and for `menagerie` Projects show the task status refreshed at least every 30 s while the Project is open
    - _Requirements: 3.5, 3.11, 5.7_

  - [x] 19.5 Write property test for the Browser IDE link predicate
    - **Property 27: Handoff selection and the Browser IDE link** (link predicate, using `fast-check`)
    - **Validates: Requirements 10.3, 10.4**

  - [x] 19.6 Extend `web/tests/e2e/workbench.spec.ts` with Phase 3 cases
    - Controls hidden when the IDE is disabled, "Open in Browser IDE" link shown and hidden, and the handoff status unchanged after following the link
    - _Requirements: 5.12, 10.3, 10.4, 10.5_

- [~] 20. Final checkpoint - Phase 3 gate
  - Ensure all tests pass, ask the user if questions arise.
  - Run the live NetworkPolicy probe, generate `docs/workbench/evidence-phase-3.md`, and leave the Browser_IDE disabled until the Owner reviews the evidence.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Each property test is its own sub-task and carries its property number and the requirements it validates.
- Each phase's tasks run only after the previous phase's checkpoint is accepted by the Owner. This is the delivery gate from Requirement 11.3 to 11.5, and the dependency graph below orders waves to match.
- Property tests are tagged in code with `# Feature: opencode-menagerie-workbench, Property N: <property text>` and run at least 100 examples. Python properties use `hypothesis` and web properties use `fast-check`.
- Properties 19, 20, 21 and 26 cover code shared across phases and are tested in the phase in which the code first ships.
- Some requirements are verified by integration and manifest checks rather than properties, such as NetworkPolicy enforcement, PVC binding in 60 s, NFS ownership, pod rescheduling and the NFS-unreachable Pending state. These are run by the Owner at each phase gate and listed in the design's Testing Strategy.
- Open assumptions to confirm are in the design's "Requirement conflicts and gaps" section, in particular the Req 8.2 enforcement limit and the Req 3.11 `menagerie-status` mechanism.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2.1", "2.3", "3.3", "5.1", "5.3", "5.5", "6.1", "7.1"] },
    { "id": 2, "tasks": ["2.2", "2.4", "2.5", "2.6", "3.1", "3.4", "5.2", "5.6", "6.2", "6.3"] },
    { "id": 3, "tasks": ["3.2", "5.4", "5.7", "6.4", "6.5"] },
    { "id": 4, "tasks": ["3.5", "3.6", "6.6"] },
    { "id": 5, "tasks": ["3.7", "3.8", "3.9", "3.10", "4.1"] },
    { "id": 6, "tasks": ["4.2"] },
    { "id": 7, "tasks": ["9.1", "11.1", "11.3", "12.1", "13.1", "13.3"] },
    { "id": 8, "tasks": ["9.2", "9.3", "9.4", "9.5", "10.1", "11.2", "12.2", "12.3", "13.2", "13.4", "13.5"] },
    { "id": 9, "tasks": ["10.2", "10.3", "10.4", "10.5", "10.6", "11.5", "11.6", "11.7", "13.6", "13.9"] },
    { "id": 10, "tasks": ["11.4", "13.7"] },
    { "id": 11, "tasks": ["11.8", "11.9", "13.8"] },
    { "id": 12, "tasks": ["13.10"] },
    { "id": 13, "tasks": ["15.1", "15.2", "17.1", "18.1", "18.2", "18.3", "19.1", "19.3"] },
    { "id": 14, "tasks": ["15.3", "15.5", "15.7", "15.8", "15.10", "17.2", "18.4", "18.5", "19.2", "19.5"] },
    { "id": 15, "tasks": ["15.4", "15.6", "15.11", "19.4"] },
    { "id": 16, "tasks": ["15.9", "16.1", "16.2"] },
    { "id": 17, "tasks": ["19.6"] }
  ]
}
```
