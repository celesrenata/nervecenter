# Personal-Ops — Contracts-and-Evidence Audit

| Field | Value |
| --- | --- |
| Report | `specs/personal-ops-recovery/audit/contracts-evidence.md` |
| Date | 2026-10-27 (UTC) |
| Worker | worker-4 |
| Method | Static-only audit (source, manifests, tests). No runtime, no in-cluster, no browser verification performed for this report. |
| Traceability | Every claim cites a path (and line range where useful) in the worker-4 checkout. |

---

## 1. Spec-pack status — ABSENT (critical gap)

**Finding S1-1 (critical):** `specs/personal-ops-recovery/` does not exist in this workspace.
This audit file itself is the only content under that path (created by this report).

Evidence:
- A listing of `specs/` shows exactly eleven sibling spec directories, none named
  `personal-ops-recovery`: `gap-closure-t003/`, `mobile-apps/`, `mobile-quality-audit/`,
  `nervcenter-uplift/`, `nervecenter-aws-platform/`, `nervecenter-deploy/`,
  `personal-reasoning-policy-closure/`, `phase0-integrity/`, `sso-integration/`,
  `t001-stabilization/`, `web-quality-audit/`.
- `docs/implementation-status.yaml` marks the general NerveCenter §95 workstreams
  ws-01…ws-06 complete. Those markers describe §95 work, **not** the personal-ops spec pack,
  and are therefore **inadmissible** as Section-1 markers for this pack.

Per the audit rule: with zero traceable spec-pack markers, Section 1 of the personal-ops
spec pack is **formally absent**, and the absence is recorded as a **critical gap**.

Unresolvable hypotheses (neither confirmable from worker-4):
- (a) the pack exists in another worker's workspace or the canonical checkout;
- (b) the pack was never authored here.
Resolving either requires access outside worker-4, which this audit does not have.

Consequence: contract statements in Sections 2–4 below are derived from the *implemented*
surface (code, manifests, tests), not from a normative spec document. Where the code
references a spec section (§10, §16, §30, §45, §46, §54, §61, §69, §102/R112, T-H32),
that reference is a marker carried in code comments and tests; the underlying pack that
would define those sections is the missing artifact from S1-1.

---

## 2. Contract inventory (implemented surface)

### 2.1 Backend API routes (160+ confirmed, static survey)

Confirmed route groups (FastAPI app, `backend/`):

| Group | Routes |
| --- | --- |
| Health / versions | `/api/health`, `/api/v1/health`, `/api/versions`, `/api/current` |
| Auth / SSO | `/api/auth/sso/config`, `/api/auth/login`, `/api/auth/me`, `/api/auth/logout`, `/api/auth/pairing/initiate`, `/api/auth/pairing/confirm` |
| Approvals | `/api/approvals/*` |
| Entities | `/api/entities/*` |
| Waiting-on | `/api/waiting-on` (list), ingest, get, resolve, follow-up |
| Communications | `/api/communications/*` |
| Voicemail | `/api/voicemail/*` |
| Overview | `/api/overview/*` |
| Relationships | `/api/relationships/*` |
| Calendar | conflict, free-time, suggest, get, delete |
| Goals | `/api/goals/*` |
| Events | `/api/events/*`, plus WebSocket `/api/events/ws` |
| Systems | `/api/systems/` |
| Evidence | `/api/evidence/*` |
| Contacts | list, ingest, get, vip |
| Claims | `/api/claims` (501) |
| Context | `/api/context/ask` |
| Drafts | `/api/drafts/*` |
| Incidents | `/api/incidents/*` |
| Models | `/api/models/history/` |
| Briefing | get, operational |
| People | list, identities/ingest, get, vip, merge |
| Email | list, ingest, get, acknowledge, draft |
| Tasks | `/api/tasks/*` |
| Attention | `/api/attention/*` |
| Observability | health, metrics, traces |

### 2.2 MCP — 11 R112 personal tools (29-tool catalogue)

`mcp/tools_module.py` exposes 29 tools total (11 personal R112 + 18 §45 platform tools).
Each personal tool dispatches to a canonical backend route:

| Tool | Method / Path |
| --- | --- |
| `get_attention` | GET `/api/attention` |
| `get_daily_brief` | GET `/api/briefing` |
| `get_person` | GET `/api/people/{id}` |
| `get_conversation` | GET `/api/conversations/{id}` |
| `search_communications` | GET `/api/communications` |
| `get_commitments` | GET `/api/commitments` |
| `get_waiting_on` | GET `/api/waiting-on` |
| `create_followup` | POST `/api/followups` |
| `snooze_attention` | POST `/api/attention/{id}/snooze` |
| `resolve_attention` | POST `/api/attention/{id}/resolve` |
| `draft_reply` | POST `/api/drafts` |

Contract markers: §29 trace header (`Nerve-Trace-Id`) on personal calls; §45 Tool schema;
explicit no-send semantics for `create_followup` / `draft_reply` ("sends nothing" /
"never sends").

### 2.3 CLI — 17 commands (`cli/main.py`)

`status`, `ask`, `entity`, `changes`, `goal`, `task`, `agents`, `compute`, `routes`,
`incident investigate`, `incident show`, `pause-agents`, `stop-mutations`,
`attention {list,show,resolve,suppress,acknowledge,snooze}`, `today`, `commitments`,
`waiting`, `brief`, `people`, `communication {list,search}`.
Envelope handling per §46 `ListResponse` unwrap lives in `cli/_helpers.py`.

### 2.4 Apple / Swift surfaces (`apple/Shared/Sources/NerveApps/`)

| File | Contract surface |
| --- | --- |
| `PersonalSurfaces.swift` | TodayRow, AttentionInput, FollowUpInput, WaitingInput, CommunicationRow/Thread, PersonRow, IdentityRow (VIP toggle → POST `/api/people/{id}/vip`), WaitingRow, BriefingAction/Waiting/Event rows, BriefingData |
| `WatchAttentionEngine.swift` | AttentionFeedState, AttentionRankedItem; rank/apply/localState/isNow/isOpen/settle/isAwake |
| `WatchAttentionJSON.swift` | AttentionWireItem (null-exclude encode); AttentionFile: missing/malformed → no state (§61 honesty); AttentionActionRecord |
| `DevicePairing.swift` | PairingOutcome, PairingFlow (run/loadExisting/revoke) |
| `ComplicationLadder.swift` | ComplicationPriority, ComplicationInputs, ComplicationItem (offline §69) |
| `StaleState.swift` | StalenessReport/Policy, asOfLabel |
| `Widgets.swift` | ComputeWidgetRow, WidgetTile, Small/Medium/Large content |
| `NotificationPolicy.swift` | MobileNotification, classify/evaluate |
| `RealtimeClient.swift` / `WebSocketRealtimeClient` | §54 WebSocket client, backoff, subscribe/unsubscribe |
| `BriefingView.swift` | iOS briefing view (§30 honest closer when empty) |
| `WatchGoalViews.swift` | WatchAlertView, WatchQueryView, WatchAcknowledgeView |
| `URLSessionAPIClient.swift` | APITransport production implementation |
| `CachedState.swift` | §69 compact state |
| `Claim.swift`, `Connector.swift`, `Approval.swift` | Core value types; `Event.swift` carries the integrity-hash contract |

### 2.5 Web e2e surface (Playwright, `web/tests/e2e/`)

| Spec | Contract evidence |
| --- | --- |
| `views.spec.ts` | AC005: every auth-gated view (12+) renders without console errors |
| `views-a.spec.ts` | Overview, Systems, Task DAG, Goals on real data, no hardcoded fixtures |
| `views-b.spec.ts` | Incidents; Decisions (DC-1 alert on 501); Timeline (TL-1 real events, 503 alert, honest empty); Inference; Agents (AG-2 aria-label, AG-3 create + 409) |
| `briefing.spec.ts` | BF-1 (four §30 sections), BF-2 (kind selector re-fetch), BF-3 (empty → §30 honest closer), BF-4 (nav link) |
| `events-ws.spec.ts` | `GET /api/events/ws` handshake + frame protocol (status/update/error) |
| `auth-transport.spec.ts` | §102: realApi rejects non-2xx with ApiError |
| `waiting-on.spec.ts` | WO-1+: seeded requests, elapsed badges, row actions |
| `login-shell.spec.ts` | LG-1…LG-4 (no default creds advertised; invalid → "Invalid credentials."); D-1/D-4 aria-current |
| `settings.spec.ts` | Three sections; global attributes persist across reload |
| `ask.spec.ts` | Ask round-trip, policy-denial fields, zero console errors |

### 2.6 Persistence / deployment contract surface

`deployments/kubernetes/base/*` (namespace `nervecenter`) and `deployments/local/docker-compose.yaml`:
Postgres (StatefulSet + PVC + Service), NATS (Deployment + Service), migration Job,
API/MCP/Web Deployments + Services, OAuth gate, Ingress, ConfigMap, secrets example.

---

## 3. Evidence mapping — contract → tests

### 3.1 Backend (pytest)

| Contract area | Test file(s) | Key assertions |
| --- | --- | --- |
| §10 frozen event schema + T-H32 events bus API | `tests/backend/test_events_api.py` | bus-backed listing; limit bounds the window; entity filter by subject; honest empty on empty stream; 503 `event_bus_unavailable` on unreachable bus; 503 on missing stream; read timeout → 503; not-in-window → 404 (not fabricated); bus-not-configured → 404/empty as applicable; query layer raises EventBusError when disconnected; partial window on late failure; schema frozen; publisher works on frozen events; no hardcoded fixtures; real app without bus → 200 honest empty |
| Waiting-on plane | `tests/backend/test_waiting_on.py` (28+) | defaults; source/status enums; idempotent ingest by `request_id` (201 new / 200 duplicate); replay does not stack a second AttentionItem; attention factor mentions person + description; elapsed-days (0/1/3+/clamp-future); list ordering; status filters (all, invalid → 422); resolve closes AttentionItem; follow-up bumps count and returns draft; 404s; 422s (empty person/description, bad source); seed; reset |
| Voicemail | `tests/backend/test_voicemail.py` | source enum; same number → same thread; different numbers → different threads; display format; attention item refreshed not duplicated |
| Email plane | `tests/backend/test_email.py` (19) | defaults; ingest creates thread; 201/200 replay; one thread per sender+subject; chronological; reply joins thread; sent clears unread; 404 unknown thread; 422 bad direction; new thread → attention item; second inbound refreshes; acknowledge resolves; draft reply (question / no-inbound); recency-sorted list; null sender name; demo seed; service reset |
| Messages plane | `tests/backend/test_messages.py` (18) | defaults; ingest creates thread; 201/200 replay; one thread per peer per channel; chronological; outbound clears unread; 404/422; new conversation → attention; refresh-not-duplicate; acknowledge resolves; person registration resolves existing+future; null peer name; draft reply; list filter by channel; reset |
| Calendar plane | `tests/backend/test_calendar.py` (20) | defaults; all-day; 201/200 replay; invalid window 422; availability free/blocked/partial-blocked; adjacent no-conflict; all-day blocks full day; earliest free slot; no slot when full/empty-horizon; suggest blocks+proposes; suggest late conflict; get round-trip; delete→404; list window filter; reset |
| Attention item model | `tests/backend/test_attention.py` | defaults, priority range, state enum |
| Commitment detection | `tests/backend/test_detection.py` | recall (6 §16 examples); fields; outgoing-only; attention seam (one item per finding; re-detect refreshes, never duplicates) |
| Personal policy | `tests/backend/test_personal_policy.py` | sensitivity classification (health → personal) |
| Write plane (gap-3) | `tests/backend/test_write_api.py`, `tests/backend/test_write_routes_gap3.py` | goal/task create → 503; incident create/transition → 503; list → 200 empty (honest non-implementation) |
| Gap-closure wiring | `tests/backend/test_gap_closure.py` | agents use real registry (fixture IDs gone, canonical alias); systems endpoint unwired → 501, wired → live rows incl. NATS row; overview real counts equal to agents endpoint; events WebSocket no-bus → error frame + close, connected → status frame; startup wiring of systems |
| Schema invariants | `tests/backend/test_schema_param_types.py` | every registered table present; every `id` column String; FK/id columns String; temporal columns `DateTime(timezone=True)`; no UUID; no string temporal; goal/task deadline preservation; native datetime compilation; structured 500 handler |
| Agents registry | `tests/backend/test_agents.py` | registry exists; 14 native agent count |
| Follow-ups | `tests/backend/test_followups.py` | waiting-on service can build/track a follow-up |

### 3.2 MCP (offline, `httpx.MockTransport`)

`tests/mcp/test_personal_tools.py` (11 tests):
- `test_all_tools_exactly_nine_twenty_nine` — catalogue size 29 (11 personal + 18 §45);
- `test_new_descriptor_metadata` — descriptor shape;
- `test_required_args_match_contract` — required args per tool;
- `test_no_send_semantics_in_descriptions` — `create_followup` / `draft_reply` carry "sends nothing" / "never sends";
- `test_dispatch_methods_paths_and_shapes` — each tool's exact method/path/args;
- `test_optional_args_omitted` — optional args never appear on the wire when omitted;
- `test_missing_required_arg_is_tool_error`;
- `test_unknown_tool_is_tool_error_not_exception`;
- `test_permission_metadata_not_supported_by_existing_schema` — §45 Tool schema has no permission field (contract blocker, see Finding F3);
- `test_trace_header_on_personal_calls` — §29 `Nerve-Trace-Id`;
- `test_base_url_env_override`.

### 3.3 Web e2e / Apple

Web: mapping in §2.5 (each spec asserts its contract rows). Apple: static survey in §2.4;
no Swift test runner was executed for this audit (static-only constraint).

---

## 4. Deployment / persistence evidence (static)

### 4.1 Postgres — durable (in-cluster and local)

- `deployments/kubernetes/base/postgres-statefulset.yaml`: image `pgvector/pgvector:pg16`,
  namespace `nervecenter`, `PGDATA=/var/lib/postgresql/pgdata` mounted via
  `subPath: pgdata` on PVC `postgres-data` → data survives pod restart.
  Credentials `nervcenter:nervcenter`; probes `pg_isready -U nervcenter -d nervcenter`
  (readiness + liveness).
- `deployments/kubernetes/base/postgres-pvc.yaml`: 10Gi, StorageClass `longhorn`,
  `ReadWriteOnce`.
- `deployments/local/docker-compose.yaml`: named volume `pgdata:/var/lib/postgresql`
  → durable locally as well.
- Canonical URL match: `backend/persistence/connection.py` L55–57
  `postgresql+asyncpg://nervcenter:nervcenter@localhost:5432/nervcenter`.

### 4.2 NATS — EPHEMERAL in-cluster (material gap)

- `deployments/kubernetes/base/nats-deployment.yaml`: image `nats:2.10-alpine`,
  args `--jetstream --store_dir /data/jetstream --http_port 8222`, probe `GET /varz:8222`.
  Volume `jetstream` is `emptyDir: {}` → **JetStream state is lost on pod restart.**
- `deployments/local/docker-compose.yaml`: named volume `jetstream:/data/jetstream`
  → persistent locally.
- Contract tension: the events API (T-H32, `tests/backend/test_events_api.py`) treats
  the JetStream bus as the source of truth for `/api/events` history. If event durability
  is contractual, the in-cluster `emptyDir` breaks it silently; if it is not contractual,
  the discrepancy is documentation-only. Neither reading is resolvable from worker-4
  because the spec pack (S1-1) is absent.

### 4.3 Migration Job

- `deployments/kubernetes/base/migrate-job.yaml`: Job `nervecenter-migrate`, ns
  `nervecenter`, command `python -m backend.persistence.migrate upgrade`
  (alembic `upgrade head`), image `registry.celestium.life/nervecenter/api:latest`
  with `imagePullPolicy: Always`, `envFrom` Secret `nervecenter-env`,
  `backoffLimit: 5`, `activeDeadlineSeconds: 120`, `restartPolicy: Never`.
- Ordering discipline (from `docs/DEPLOYMENT.md` and audit notes): run **before**
  rolling the API Deployment; alembic is idempotent, so no-op migrations complete
  immediately.

---

## 5. Do-not-touch candidates (load-bearing contracts)

| Path | Reason |
| --- | --- |
| `backend/events/models.py` | Frozen §10 event schema; publisher and query layers depend on exact shape |
| `backend/persistence/connection.py` | Canonical DB URL + engine/pool config; credential match with manifests |
| `mcp/tools_module.py` | 29-tool catalogue; MCP contract surface |
| `cli/_helpers.py` | §46 `ListResponse` envelope unwrap shared by all list commands |
| `apple/Shared/Sources/NerveModels/Event.swift` | Integrity-hash contract for Apple event replay |
| `deployments/kubernetes/base/postgres-statefulset.yaml` | PGDATA `subPath` pinning is what makes PVC contents survive restarts |

---

## 6. Findings

- **F1 (critical) — Spec pack absent.** `specs/personal-ops-recovery/` does not exist in
  worker-4; §95 workstream markers in `docs/implementation-status.yaml` are inadmissible.
  Section 1 of the pack is formally absent. See §1.
- **F2 (material) — NATS JetStream durability mismatch.** In-cluster NATS uses
  `emptyDir` (state lost on restart) while local compose persists, and the events API
  contract reads history from the bus. Residual risk: event history is durable only
  locally. See §4.2.
- **F3 (contract blocker) — MCP Tool schema lacks a permission field.**
  `test_permission_metadata_not_supported_by_existing_schema` documents that §45's
  Tool schema cannot express permission metadata; any permissioned-tool contract is
  currently unexpressible without a schema change.
- **F4 (declared, not delivered) — write-plane 503s.** Goal/task create and incident
  create/transition return 503 by design (tested in `test_write_api.py` /
  `test_write_routes_gap3.py`). The CLI/MCP surfaces that reference them therefore
  exercise honest non-implementation, not silent failure.
- **F5 (deployment hygiene) — unpinned API image.** `migrate-job.yaml` (and the API
  deployment it mirrors) uses `api:latest` with `imagePullPolicy: Always`; a rebuild
  changes runtime behavior without a tag-level diff. The mcp Deployment is the opposite
  discipline (pinned tag) per deployment rules, making the API inconsistent.

---

## 7. Coverage summary and verification limits

**Strongest claims** (fully backed by static evidence):
1. Frozen §10 event schema + T-H32 events API degradation semantics — complete test
   matrix in `tests/backend/test_events_api.py` covering 200/404/503 branches.
2. Postgres durability chain: StatefulSet `subPath` pinning → PVC (`longhorn`, 10Gi,
   RWO) → credential match with `backend/persistence/connection.py` → `pg_isready` probes.
3. MCP personal-tool dispatch: exact method/path/arg shape for all 11 tools, offline.

**Weakest claims:**
1. Section 1 of the spec pack — **absent**, critical (F1); all §-numbered references are
   code-carried markers, not verified against a normative document.
2. In-cluster event durability — contradicted by `emptyDir` (F2); contract intent
   unverifiable while the pack is missing.

**Coverage:** contract inventory (§2) spans backend routes, 29 MCP tools, 17 CLI
commands, 14 Apple surface files, 10 web e2e specs, and 18 k8s manifests + local
compose. Evidence mapping (§3) ties every contract area to named test files; backend
and MCP tests are the direct evidence, web e2e is declared evidence (Playwright), and
Apple surfaces are static-survey only.

**Verification limits (static-only):**
- No runtime, in-cluster, browser, or Swift-test execution was performed.
- The two hypotheses about where the spec pack lives (other worker / never created)
  are unresolvable from worker-4.
- Line budget: this report is within the 400-line cap (verified with `wc -l`).
