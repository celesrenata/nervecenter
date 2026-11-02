# Per-Agent Inference Control — shared contract (Batch 4)

Two workers implement this in parallel (backend A, web B). **This file is the
single source of truth for the wire shapes.** Do not deviate; if you must,
record it in your final report.

## Problem
NerveCenter inference is a single process-wide default
(`AskService(model="nerve-ask", provider="omniroute")`,
`backend/context/ask.py:298-299`). There is no way to control which
model/endpoint inference travels through, per agent or per request.
`web/docs/FEATURES.md:52-53` promises "Query the inference engine / model
selection" and spec §6 puts Agent Workforce → Inference Policy Engine →
OmniRoute. The control surface is missing.

## Wire shapes (frozen)

### 1. `GET /api/inference/available` → 200
```json
{
  "accepted": true, "executed": true, "service": "inference",
  "request_id": "...", "timestamp": "...",
  "result": {
    "providers": [
      {"provider":"omniroute","model_id":"gpt-4o-mini","base_url":null,
       "key_configured":true,"capabilities":["generation"],"default":true}
    ],
    "models": ["gpt-4o-mini", "claude-sonnet-4"],
    "default": {"provider": "omniroute", "model": "nerve-ask"}
  }
}
```
- `providers` = the exact rows of the existing `GET /api/inference/providers`
  (`build_provider_pool()` sanitized entries + the omniroute default entry).
- `models` = `NERVE_INFER_MODELS` (JSON array of strings) when set, else the
  `model_id` values of configured providers, else `[]`.
- `default` = `NERVE_INFER_DEFAULT_PROVIDER` / `NERVE_INFER_DEFAULT_MODEL`
  env; when absent → `"omniroute"` / `"nerve-ask"` (AskService constructor
  defaults). Keys stay env-only; never echo key values.
- Honest empty lists when nothing is configured. No invented providers.

### 2. `POST /api/context/ask` body gains two OPTIONAL fields
```json
{"question": "...",
 "agent": "planner",
 "inference": {"model": "x", "provider": "omniroute"}}
```
`AskRequest` keeps `extra: "forbid"`. `agent` is `str | None` (name of a native
agent type or a persisted agent id). `inference` is a nested object with
optional `model` / `provider` strings.

Route primary provider/model resolution order (applied in `AskService.ask`):
1. service-level default (`self._model` / `self._provider`);
2. binding for `agent`, when `agent` is given: native registry store, else
   persisted instance `inference` jsonb, else native manifest `inference`;
3. request `inference` override (applied last, wins).
Partial override allowed: `{"model": "x"}` keeps provider from step 2.
Unknown provider/model is allowed (honest passthrough to OmniRoute).
The response body gains one audit key in all shapes:
`"inference": {"provider": "...", "model": "...", "source":
"request"|"agent"|"default"}`. Existing shapes (answered / policy_denied /
unavailable) are otherwise unchanged.

### 3. Agent inference binding
Shape of a binding (jsonb / object): `{"provider": str, "model": str}` —
both optional (a `null` binding means "use default").
- **Native types** (in-process store): `AgentRegistry.set_inference(agent_type,
  binding | None)` and `AgentRegistry.inference(agent_type) -> dict | None`.
  Stored on the manifest via an optional `inference` field added to
  `AgentManifest` (default None). Registry is process-wide; no DB.
- **Persisted instances**: new nullable jsonb column `inference` on the
  `agents` table (alembic `014_agent_inference.py`, `down_revision =
  "013_bill_series"`). `AgentCreateRequest.inference: dict | None`,
  `AgentUpdateRequest.inference: dict | None` (partial update: absent/null
  leaves unchanged). Row projection includes `"inference": {...} | None`.
- Native rows projected by `_manifest_to_row` include
  `"inference": <binding or None>` too.

### 4. `PATCH /api/agents/{id}` + `POST /api/agents` accept `inference`;
`POST /api/inference/agents/{agent_id}/inference` is NOT added — reuse the
agent endpoints. (Keeps surface small.)

## Backend scope (worker A)
- `backend/inference/providers.py`: add `available_models()` +
  `default_inference()` helpers (env-driven, honest).
- `backend/api/endpoints/inference.py`: add `GET /available`.
- `backend/context/ask.py`: `AskRequest` fields; `ask()` resolves
  provider/model via the order above; echo `inference` in response body.
- `backend/agents/registry.py`: `AgentManifest.inference` field +
  `AgentRegistry.set_inference` / `.inference`.
- `backend/api/endpoints/agents.py`: request models + `_manifest_to_row`
  include `inference`.
- `backend/services/incident_agent.py`: `create`/`update`/`_agent_to_row`
  handle `inference`.
- `backend/persistence/models/incidents_agents.py`: `inference` jsonb column.
- `backend/persistence/alembic/versions/014_agent_inference.py`.
- Ask pipeline resolution helper (registry + persisted instance lookup via
  a small `AgentInferenceResolver` that the ask endpoint or service uses —
  keep the `AskService` pure; do the lookup in `backend/api/endpoints/context.py`
  or a thin service and pass the resolved provider/model into
  `AskService.ask(..., provider=, model=)` or set on the request).
- Tests: `tests/backend/test_inference_available.py`, extend
  `tests/backend/test_ask_pipeline.py` (override + agent binding + audit
  echo), extend agents tests for `inference` field round-trip.

## Web scope (worker B)
- `web/src/api/inference.ts`: add `getAvailable()` typed
  `{providers, models, default}`.
- `web/src/api/agents.ts`: `Agent.inference: {provider?, model?} | null`,
  `AgentUpdatePayload.inference`, create payload.
- `web/src/api/client.ts` `postAsk(message, opts?: {agent?, inference?})`.
- `web/src/views/AgentsView.tsx`: per-agent inference control — a row action
  / inline editor (provider + model selects sourced from
  `inferenceApi.getAvailable()`) that PATCHes the agent; native types use
  `PATCH /api/agents/{type}` when a DB row exists, else the in-process
  native binding endpoint is NOT available — for native types, show the
  binding read-only if none, and offer it only on persisted agents.
  (Keep it honest: native manifest binding is process-only; if the agent id
  resolves to a native type only, the UI shows a "native default" badge.)
- `web/src/views/AskView.tsx`: optional model/provider picker (selects from
  `getAvailable()`) + agent select (from `agentsApi.list`), passed to
  `postAsk`. Honest: picker is collapsed/optional; default = service default.
- `web/src/views/InferenceView.tsx`: add an "available models" section
  (models + default) beside the existing providers list.
- `web/src/views/LoginView.tsx`: after SSO work the view is stale —
  (a) when SSO is enabled, lead with the "Sign in with NerveCenter SSO"
  button (issuer display name from `/api/auth/sso/config` when present);
  (b) move the break-glass form under a collapsed "Local admin" disclosure
  (native `<details>` or toggle) with a clear label; it is NOT the primary
  path. Keep all existing LG-1…LG-4 invariants (no default-credential hint,
  aria-live errors, pending button). If `sso/config` fails, keep current
  behavior (button hidden; form stays primary).
- Tests: extend `web/tests/e2e/` for ask model picker (a small unit test is
  acceptable) and login SSO-first layout if a harness exists; keep existing
  green.

## Definition of done (both workers)
- Backend: `pytest tests/backend -q` green (run at least the touched files +
  the ask/agents/inference suites).
- Web: `tsc --noEmit` clean (use the web project's configured checker),
  and any e2e you touch passes.
- No fabricated data anywhere; honest degradation when env/providers absent.
- No secrets in code; env var names only.
