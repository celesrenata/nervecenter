# Tasks: Personal Reasoning & Policy Closure (G-1)

**Spec:** [`spec.md`](./spec.md) · **Plan:** [`plan.md`](./plan.md)
**Status:** done (2026-10-20) · **Created:** 2026-10-14

This task file is the executable work breakdown for AC-R01–AC-R18. The feature is intentionally limited to the in-memory backend, policy interfaces, flat HTTP API, MCP tools, and CLI surfaces described by the spec. Real OmniRoute transport, Apple personal surfaces, durable personal persistence, and device testing remain outside this feature.

## Task Board

| ID | Title | AC | Depends on | Owned paths | Verification |
|---|---|---|---|---|---|
| R101 ✅ cdb2539 | Person, identity, communication, and conversation foundation | AC-R01, AC-R02 | — | [`backend/communications/`](../../backend/communications/), [`backend/api/models.py`](../../backend/api/models.py), [`tests/backend/test_communications.py`](../../tests/backend/test_communications.py) | Models, idempotent identity upsert, conservative resolution, explicit merge, and API contract tests |
| R107 ✅ 90f5009 | Personal policy foundation: firewall, labels, and inference routing | AC-R12, AC-R13, AC-R14 | R101 label contract | [`backend/policy/`](../../backend/policy/), [`backend/context/compiler.py`](../../backend/context/compiler.py), [`tests/backend/test_personal_policy.py`](../../tests/backend/test_personal_policy.py) | Firewall fixtures and six-case PDP matrix |
| R102 ✅ b926716 | Normalize existing ingest planes into communications | AC-R03 | R101, R107 | [`backend/messages/`](../../backend/messages/), [`backend/email/`](../../backend/email/), [`backend/voicemail/`](../../backend/voicemail/), [`backend/communications/normalize.py`](../../backend/communications/normalize.py) | §62 cross-channel scenario: one person, three communications, one conversation |
| R103 ✅ f036960 | Deterministic commitment, request, and deadline detection | AC-R04, AC-R05, AC-R06 | R102 | [`backend/communications/detection/`](../../backend/communications/detection/), [`tests/backend/test_detection.py`](../../tests/backend/test_detection.py) | At least 15 tests, recall fixtures, noise precision, events, and attention source |
| R104 ✅ bdc131f | Follow-up model, engine, due scheduling, and actions | AC-R07 | R103 | [`backend/followups/`](../../backend/followups/), [`backend/api/endpoints/followups.py`](../../backend/api/endpoints/followups.py), [`tests/backend/test_followups.py`](../../tests/backend/test_followups.py) | Due follow-up attention item, lifecycle events, snooze/resolve propagation, waiting-on regression |
| R105 ✅ 795bf38 | Draft store, API, fallback drafter, and policy-gated assistance | AC-R08 | R101, R107 | [`backend/drafts/`](../../backend/drafts/), [`backend/api/endpoints/drafts.py`](../../backend/api/endpoints/drafts.py), [`tests/backend/test_drafts.py`](../../tests/backend/test_drafts.py) | Draft CRUD/discard, deterministic fallback, policy check, and no API transition to `sent` |
| R108 ✅ 3da8c2b | Attention budget, quiet periods, escalation, and adaptive escalation | AC-R15 | R103, R107 | [`backend/attention/`](../../backend/attention/), [`backend/briefing/service.py`](../../backend/briefing/service.py), [`tests/backend/test_attention_policy.py`](../../tests/backend/test_attention_policy.py) | Budget bundling, quiet notification suppression, deadline promotion, local preferences |
| R106 ✅ e0217d5 | End-to-end detection/follow-up/attention wiring | AC-R06, AC-R07, AC-R15 | R104, R105, R108 | [`backend/communications/detection/wiring.py`](../../backend/communications/detection/wiring.py), [`tests/backend/`](../../tests/backend/) | §61 scenario progresses from ingest through attention state and audit fields |
| R109 ✅ 1521681 | §63 mapping documentation and Daily Operational State | AC-R09, AC-R17 | R104, R105 | [`docs/nervcenter-spec.md`](../../docs/nervcenter-spec.md), [`backend/briefing/`](../../backend/briefing/), [`tests/backend/test_operational_state.py`](../../tests/backend/test_operational_state.py) | Mapping table and operational route with all eight sections |
| R110 ✅ 9c2cd96 | Cross-channel search and memory extraction | AC-R16 | R102, R109 | [`backend/communications/search.py`](../../backend/communications/search.py), [`backend/communications/memory.py`](../../backend/communications/memory.py), [`tests/backend/test_personal_search.py`](../../tests/backend/test_personal_search.py) | §50 query fixtures, provenance-bearing durable facts, routine-conversation negative test |
| R112 ✅ 7eaeec1 | §64 MCP tools: 18 to 29 | AC-R10 | R104, R105, R109, R110 | [`mcp/tools_module.py`](../../mcp/tools_module.py), [`tests/mcp/`](../../tests/mcp/) | Exact names/count, schemas, paths, methods, and wire behavior |
| R113 ✅ a1ba1ce | §65 personal CLI commands | AC-R11 | R109, R110 | [`cli/commands/`](../../cli/commands/), [`cli/main.py`](../../cli/main.py), [`cli/output.py`](../../cli/output.py) | Seeded-backend smoke for every command, JSON mode, and exit codes |
| R111 ✅ (this commit) | Final evidence gate and status record | AC-R18 | R101–R113 | [`docs/implementation-status.yaml`](../../docs/implementation-status.yaml), this file | Full non-integration suite, MCP 29-tool suite, CLI smoke, §61/§62 scenarios, evidence log |

R101–R106 share the communications/ingest backend ownership and are executed sequentially. R107 can begin with R101's `Communication.sensitivity` contract and may run in parallel with R102. R108 follows detection contracts but may be implemented in parallel with R104/R105 once policy interfaces exist. R109 and R110 are surface-layer tasks; R112 and R113 must not modify endpoint contracts and may only consume the published OpenAPI behavior.

## Global implementation rules

- Use singleton in-memory services with `reset_*` hooks, matching the existing personal planes.
- Preserve the flat API shape. Do not create an `/api/personal/*` alias layer; R109 documents the mapping.
- Deterministic behavior is the required path. Every detected or surfaced item has non-empty `why[]`, `source`, `classification`, and `model_used: "deterministic"` when no model is used.
- Personal content is `LOCAL_ONLY` by default. Remote inference requires an explicit policy decision and is never allowed for confidential or local-only data.
- Identity resolution is conservative: equal canonical values may link; display-name similarity alone may not merge records. Merges require an explicit API action.
- Draft endpoints never mark a draft `sent`; only the future relay send path may do so. No task in this feature auto-sends a communication.
- Do not add persistence tables, real OmniRoute transport, Apple UI, or device-signing work to this feature.
- Run the narrow task test first, then the house gate: `.venv/bin/python -m pytest tests/ -m "not integration" -q`.

## Detailed task specifications

### R101 — Person and communication foundation

**Goal:** Establish the canonical personal graph without breaking the existing VIP contacts or per-channel message-thread APIs.

**Implementation steps**

1. Add `Person`, `ContactIdentity`, `Communication`, and cross-channel `Conversation` domain models in [`backend/communications/models.py`](../../backend/communications/models.py). `Communication` must include channel, direction, sender, recipients, timestamp, thread ID, content reference, attachments, classification, sensitivity, and source.
2. Define the allowed channels (`email`, `imessage`, `sms`, `rcs`, `voicemail`) and the §36 sensitivity labels (`PERSONAL`, `PRIVATE`, `FINANCIAL`, `WORK`, `PROJECT`, `LOW-SENSITIVITY`). Raw content defaults to `LOCAL_ONLY` routing metadata.
3. Implement a singleton registry and reset hook. Re-ingesting an identity with the same normalized kind/value returns the same person and does not duplicate identities.
4. Implement value-first resolution for email addresses, phone/relay identifiers, voicemail callers, and calendar/contact values. A same-name/different-value result is `PROPOSED`, not merged.
5. Add an explicit merge operation and `POST /api/people/{person_id}/merge`; preserve existing contacts behavior by delegating VIP state to the canonical person where practical.
6. Add list/get/create/merge endpoint behavior and serialization tests.

**Acceptance mapping:** AC-R01 and AC-R02.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_communications.py -q
.venv/bin/python -m pytest tests/backend/test_communications.py -k 'identity or resolution or merge or model' -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

Required assertions include idempotent re-ingest, uncertain-match negative behavior, explicit merge, valid channel/label values, and no raw credential/content leakage in serialized identity records.

### R107 — Personal policy foundation

**Goal:** Make privacy and personal/engineering separation executable before any model-assist hook is used.

**Implementation steps**

1. Add a partition field to context sources and enforce that engineering compilation excludes personal communications by default.
2. Add deterministic sensitivity classification at ingest and allow an explicit communications API override.
3. Extend the existing PDP with an `INFER_ROUTE` decision rather than creating a second policy engine. The request contains `privacy`, `contains_personal_communications`, and `remote_provider_allowed`; the decision returns `model_tier`, `remote_allowed`, `policy_id`, and `reason`.
4. Preserve default-deny and explicit-DENY precedence. Confidential/`LOCAL_ONLY` input can use only local tiers. A remote-allowed result requires an explicit matching policy.
5. Expose a distinct future sending capability in the PDP without adding a sending endpoint or MCP tool.

**Acceptance mapping:** AC-R12, AC-R13, and AC-R14.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_personal_policy.py -q
.venv/bin/python -m pytest tests/backend/test_personal_policy.py -k 'firewall or infer or sensitivity' -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

The matrix must include at least six cases: confidential/local-only denial, personal default denial, low-sensitivity explicit allow, missing-policy default denial, explicit deny precedence, and engineering-only routing.

### R102 — Ingest normalization and §62 scenario

**Goal:** Route existing messages, email, voicemail, and calendar ingest through the canonical resolution and communication registry.

**Implementation steps**

1. Add a normalization adapter that converts each source payload into a `Communication` without changing existing source service contracts.
2. Resolve sender/peer/caller identities through R101 and group communications into one cross-channel conversation.
3. Preserve source provenance and channel-specific references; do not store raw secrets.
4. Connect email, message, voicemail, and calendar hooks with idempotent replay behavior.
5. Build the §62 Alice fixture: email at 10:00, iMessage at 10:12, voicemail at 13:40. Ensure one person, three communications, one conversation, and a callback attention item whose reason is `direct callback request`.

**Acceptance mapping:** AC-R03.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_communications.py -k 'scenario or ingest or cross_channel' -q
.venv/bin/python -m pytest tests/backend/test_messages.py tests/backend/test_email.py tests/backend/test_voicemail.py -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

### R103 — Detection engine

**Goal:** Detect commitments, requests, and deadlines deterministically and explainably.

**Implementation steps**

1. Add typed `Commitment`, `Request`, and `Deadline` findings with source communication, confidence, status, due data, and non-empty `why[]`.
2. Implement pure extractors over normalized `Communication` objects. Share deterministic date parsing for explicit dates, tomorrow, next week, by-weekday, business-hour constraints, and “remind me” forms.
3. Add the `InferenceAssist` interface, disabled by default. It may refine deterministic findings but may not be the sole detector.
4. Emit the §8 detected/update/overdue events and feed findings into the existing attention service without replacing its explainable scoring.
5. Add request/deadline/commitment endpoint contracts and audit fields.
6. Add recall-first fixtures for all six commitment examples and deadline examples, plus labeled marketing/newsletter/social noise fixtures with a stated false-positive threshold of zero for the required examples.

**Acceptance mapping:** AC-R04, AC-R05, and AC-R06.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_detection.py -q
.venv/bin/python -m pytest tests/backend/test_detection.py --collect-only -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

The detection module must have at least 15 tests and identical input must yield identical output with model assistance disabled.

### R104 — Follow-up engine

**Goal:** Turn unanswered requests and unfulfilled commitments into first-class due items.

**Implementation steps**

1. Add `FollowUp` with target kind/ID, person ID, due time, reason, created-from, status, and audit history.
2. Add create/list/get/update/complete/forget behavior and reset hook.
3. Create follow-ups from outgoing requests/commitments and the waiting-on integration, with configurable default timing.
4. Add a deterministic due evaluation hook. A due follow-up creates a `FollowUp`-sourced attention item and emits `followup.triggered`.
5. Implement Follow up, Wait, and Forget actions. Attention snooze/resolve must propagate to the live follow-up.
6. Preserve the existing waiting-on `follow-up` helper behavior and regression coverage.

**Acceptance mapping:** AC-R07.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_followups.py -q
.venv/bin/python -m pytest tests/backend/test_waiting_on.py -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

Assert `followup.created`, `followup.triggered`, and `followup.completed`, plus due-item reason text and action propagation.

### R105 — Drafts plane

**Goal:** Store auditable drafts while preserving human-controlled sending.

**Implementation steps**

1. Add a singleton `Draft` store with channel, conversation ID, communication reference, text, basis, model used, classification, status, source, and action history.
2. Implement `GET /api/drafts`, `POST /api/drafts`, `GET/PATCH /api/drafts/{id}`, and `POST /api/drafts/{id}/discard`.
3. Keep the rule-based email drafter as deterministic fallback with `model_used: "deterministic"`.
4. Add an injectable model-assisted drafter interface. Before invocation, call the R107 inference policy; deny remote use for local-only/personal data.
5. Make status transitions explicit: `DRAFT → DISCARDED`; `SENT` is not reachable through these endpoints.
6. Include basis/evidence references in every response and never claim a send occurred.

**Acceptance mapping:** AC-R08.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_drafts.py -q
.venv/bin/python -m pytest tests/backend/test_drafts.py -k 'sent or policy or fallback' -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

### R108 — Attention policy

**Goal:** Add local, user-controlled policy around the existing attention service without deleting or hiding items.

**Implementation steps**

1. Add per-user daily budget configuration for FYI/low-priority items. Bundle nine low-priority items into one briefing entry; urgent items bypass batching.
2. Add local quiet-period preferences for sleep, meetings, focus, driving, and manual DND. Quiet periods set notification suppression only; items remain in `/api/attention/now`.
3. Promote TODAY items to NOW when deadline proximity crosses the configured threshold and preserve score inputs/why data.
4. Add per-person/per-channel adaptive escalation preferences, stored locally and user-editable.
5. Expose notification state, escalation reason, and action history in audit responses.

**Acceptance mapping:** AC-R15.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_attention_policy.py -q
.venv/bin/python -m pytest tests/backend/test_attention.py -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

### R106 — End-to-end attention wiring

**Goal:** Prove that detection, follow-ups, and attention policy compose without losing provenance.

**Implementation steps**

1. Wire ingest → detection → event → attention item → follow-up creation in a deterministic integration harness.
2. Exercise the §61 progression from email request/deadline to SOON, TODAY, and NOW as the deadline approaches.
3. Verify quiet periods suppress notification flags but not NOW results, and budget bundling does not suppress urgent items.
4. Verify snooze/resolve actions retain source, why, model, classification, score inputs, and action history.

**Acceptance mapping:** AC-R06, AC-R07, and AC-R15.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend -k 'personal or detection or followup or attention_policy' -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

### R109 — API mapping and operational state

**Goal:** Complete the flat API contract and produce the §52 rollup used by CLI/MCP.

**Implementation steps**

1. Add the canonical §63 mapping table to [`docs/nervcenter-spec.md`](../../docs/nervcenter-spec.md), explicitly documenting that `/api/personal/*` is mapped rather than aliased.
2. Register all new flat endpoint modules and confirm fresh-backend empty-list envelopes return 200.
3. Add `GET /api/briefing/operational` with sections for open commitments, incoming requests, waiting-ons, upcoming deadlines, pending approvals, today’s calendar, active incidents, and blocked goals.
4. Have every section read from its owning service; do not invent aggregate data.
5. Preserve all existing routes and response shapes.

**Acceptance mapping:** AC-R09 and AC-R17.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_operational_state.py -q
.venv/bin/python -m pytest tests/backend -k 'route or endpoint or briefing' -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

### R110 — Search and memory

**Goal:** Make cross-channel communication history searchable and extract only durable memories.

**Implementation steps**

1. Add `GET /api/communications/search` with channel/person/conversation filters and deterministic query matching.
2. Support the §50 examples “who asked me for something this week” and “find the voicemail about the prescription”.
3. Extract durable appointments, decisions, commitments, project requirements, and relationships into the existing knowledge graph.
4. Attach provenance `source: communication:<id>` and preserve classification/sensitivity.
5. Ensure routine conversation creates no entities.

**Acceptance mapping:** AC-R16.

**Verification:**

```sh
.venv/bin/python -m pytest tests/backend/test_personal_search.py -q
.venv/bin/python -m pytest tests/backend/test_personal_search.py -k 'search or memory or provenance' -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

### R112 — MCP tools

**Goal:** Add exactly the 11 §64 tools to the existing 18-tool contract.

**Implementation steps**

1. Add only these names to [`mcp/tools_module.py`](../../mcp/tools_module.py): `get_attention`, `get_daily_brief`, `get_person`, `get_conversation`, `search_communications`, `get_commitments`, `get_waiting_on`, `create_followup`, `snooze_attention`, `resolve_attention`, and `draft_reply`.
2. Map each tool to its canonical flat HTTP method/path/schema. `snooze_attention` and `resolve_attention` reuse existing attention actions.
3. Preserve all existing 18 tools and assert total length exactly 29.
4. Add the distinct sending permission capability to PDP metadata, but do not add a send tool.
5. Update tool-list and wire tests in the same change.

**Acceptance mapping:** AC-R10.

**Verification:**

```sh
.venv/bin/python -m pytest tests/mcp/test_mcp_tools.py tests/mcp/test_mcp_wire.py -q
.venv/bin/python -m pytest tests/mcp/ -q
.venv/bin/python -m pytest tests/ -m "not integration" -q
```

### R113 — CLI commands

**Goal:** Expose the personal surfaces as thin, testable clients of the canonical API.

**Implementation steps**

1. Add command groups for `attention`, `today`, `commitments`, `waiting`, `brief`, `people`, and `communication`.
2. Implement `nerve attention snooze <id> <when>` and preserve the existing action/exit-code conventions.
3. Render ranked attention output with `why`; support `--json` for every command.
4. Use seeded local backend fixtures rather than hard-coded demonstration data.
5. Return exit codes 0 for success, 2 for usage/validation errors, and 127 for unavailable backend/command according to the existing CLI convention.

**Acceptance mapping:** AC-R11.

**Verification:**

```sh
.venv/bin/python -m pytest tests/cli -q
.venv/bin/python -m cli.main attention --json
.venv/bin/python -m cli.main today --json
.venv/bin/python -m cli.main commitments --json
.venv/bin/python -m cli.main waiting --json
.venv/bin/python -m cli.main brief --json
.venv/bin/python -m cli.main people Alice --json
.venv/bin/python -m cli.main communication search prescription --json
```

The command smoke must run against a seeded local backend; an unavailable service must be reported as an unverified/failed smoke rather than treated as success.

### R111 — Final evidence gate

**Goal:** Record reproducible evidence for every acceptance criterion and close the feature without papering over known boundaries.

**Implementation steps**

1. Run the complete non-integration Python suite after R101–R113 are integrated.
2. Run the MCP suite and assert exactly 29 tools.
3. Run every §65 CLI smoke command against seeded local data.
4. Run §61 and §62 scenario tests, including deterministic audit fields.
5. Add a `personal-reasoning-policy-closure` workstream to [`docs/implementation-status.yaml`](../../docs/implementation-status.yaml) with status, owned paths, commands, results, and known limitations.
6. Append the exact commands/results to this file's verification log. Do not claim a check that was not run.
7. Record explicit gaps: no real OmniRoute endpoint/transport, no Apple/device testing, and in-memory personal persistence.

**Acceptance mapping:** AC-R18 and the verification requirement for AC-R01–AC-R17.

**Verification:**

```sh
.venv/bin/python -m pytest tests/ -m "not integration" -q
.venv/bin/python -m pytest tests/mcp/ -q
.venv/bin/python -m pytest tests/backend/test_communications.py tests/backend/test_detection.py tests/backend/test_followups.py tests/backend/test_drafts.py tests/backend/test_personal_policy.py tests/backend/test_personal_search.py -q
```

The task is complete only when all required commands pass, evidence is written, and limitations are explicit.

## AC and dependency matrix

| AC | Owning task | Required predecessor | Evidence |
|---|---|---|---|
| AC-R01 | R101 | — | Models and idempotent registry tests |
| AC-R02 | R101 | — | Conservative resolution and explicit merge tests |
| AC-R03 | R102 | R101, R107 | §62 scenario test |
| AC-R04 | R103 | R102 | Extractor contract tests |
| AC-R05 | R103 | R102 | 15+ recall/precision tests |
| AC-R06 | R103 + R106 | R102, R103 | Event and attention-source integration |
| AC-R07 | R104 + R106 | R103 | Follow-up lifecycle and due-item tests |
| AC-R08 | R105 | R101, R107 | Draft API/state machine tests |
| AC-R09 | R109 | R101–R105 | Mapping and route registration tests |
| AC-R10 | R112 | R104, R105, R109, R110 | 29-tool list/wire tests |
| AC-R11 | R113 | R109, R110 | Seeded CLI smoke tests |
| AC-R12 | R107 | R101 | Firewall allow/deny tests |
| AC-R13 | R101 + R107 | R101 | Label classifier and API override tests |
| AC-R14 | R107 | R101 | Six-case inference PDP matrix |
| AC-R15 | R108 + R106 | R103, R107 | Budget/quiet/escalation tests |
| AC-R16 | R110 | R102, R109 | Search/memory/provenance tests |
| AC-R17 | R109 | R104, R105 | Eight-section operational-state test |
| AC-R18 | R111 | R101–R113 | Full evidence and status record |

No acceptance criterion is unowned. No task depends on a later task except the deliberate surface-layer ordering in R112/R113. R106 is the composition gate for the policies implemented by R104/R108 rather than a second owner of their contracts.

## Execution waves

- **Wave 1:** R101 and R107 foundation work.
- **Wave 2:** R102, then R103; R104 and R105 can proceed in parallel after R103/R107 contracts are stable; R108 proceeds once attention inputs and policy metadata are defined.
- **Wave 3:** R106, R109, and R110 in parallel where their owned paths do not overlap.
- **Wave 4:** R112 and R113 in parallel after their endpoint dependencies are published.
- **Wave 5:** R111 final integration and evidence gate.

## Verification log

*(Append command, date, result, and limitations after each task. This log is empty because implementation has not started.)*

| Task | Command | Result | Limitations / follow-up |
|---|---|---|---|
| — | — | Not run | Real OmniRoute, Apple/device testing, and durable personal persistence are out of scope per [`spec.md`](./spec.md). |
