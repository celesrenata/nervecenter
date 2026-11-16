/**
 * Agents API client (agent detail + edit for persisted instances).
 *
 * Mirrors the shape of `web/src/api/logs.ts`: a thin, typed client over the
 * frozen `backend.api.endpoints.agents` routes, built on `apiFetch` /
 * `apiErrorFromResponse` from `./client.ts` (which is NOT edited here).
 *
 *   GET  /agents                  → { data: Agent[] }          (list)
 *   GET  /agents/{id}             → Agent                      (detail)
 *   PATCH /api/agents/{id}        → Agent                      (edit)
 *
 * The legacy read paths (`/agents`) resolve BOTH native agent *types* and
 * persisted agent *instances*, so `get` works for created agents too. The
 * canonical `/api/agents` prefix is used for the new write route (PATCH) —
 * it is owned by the agents router, just like `POST /api/agents`.
 *
 * Honest error surface: every non-2xx response rejects with `ApiError`
 * (imported from `./client.ts`). There is no mock branch — an edit is a real
 * write, and faking a 200 on a failed/absent write would fabricate state.
 */

import { apiFetch, apiErrorFromResponse } from './client'
import type { ApiError } from './client'

/**
 * Inference binding for an agent (INFER_CONTROL_CONTRACT.md §3): which
 * provider / model this agent's inference travels through. Both fields are
 * optional; a `null` binding means "use the service default".
 */
export interface InferenceBinding {
  provider?: string | null
  model?: string | null
}

/** Normalized agent row (mirrors the backend web row shape). */
export interface Agent {
  id: string
  name: string
  type: string
  status: string
  description: string | null
  prompt_template: string | null
  tools: string[]
  /** Pinned Skill Hub catalog ids (NS-ADR-111). */
  skills?: string[]
  capabilities: Record<string, unknown> | null
  lastHeartbeat: string | null
  cpuUsage: number | null
  memoryUsage: number | null
  tasksCompleted: number | null
  tasksFailed: number | null
  /** Plain-language note on when the agent runs (built-ins and instances). */
  runs_when?: string | null
  /** Inference binding (`null` = use service default). Absent pre-Batch-4 rows. */
  inference?: InferenceBinding | null
  /**
   * Durable combo binding (providers feature). When set, the agent resolves
   * its provider/model through the combo at ask time (lazy) and shadows the
   * per-agent `inference` jsonb. `null`/absent = no combo binding.
   */
  combo_id?: string | null
}

/**
 * Payload for `POST /api/agents` — create a durable agent instance.
 * `name` and `type` are required; everything else is optional.
 */
export interface AgentCreatePayload {
  name: string
  type: string
  description?: string | null
  prompt_template?: string | null
  tools?: string[] | null
  skills?: string[] | null
  inference?: InferenceBinding | null
  /** Optional durable combo binding (id of an inference combo). */
  combo_id?: string | null
}

/**
 * Payload for `PATCH /api/agents/{id}` — a *partial* update. Every field is
 * optional; a `null`/omitted value leaves the attribute unchanged. Only
 * `extra: "forbid"` is enforced server-side, so unknown keys are rejected.
 */
export interface AgentUpdatePayload {
  name?: string | null
  description?: string | null
  prompt_template?: string | null
  tools?: string[] | null
  /** Pinned Skill Hub ids; replaces the list when supplied. */
  skills?: string[] | null
  inference?: InferenceBinding | null
  /**
   * Durable combo binding. A non-empty string rebinds the agent to that
   * combo; an empty string ``""`` clears it; omitted leaves it unchanged.
   */
  combo_id?: string | null
}

export { ApiError }

/** Cadence for one agent (`PUT /api/agents/{id}/schedule`). */
export interface AgentSchedule {
  agent_id?: string
  mode: 'interval' | 'daily' | 'on_demand'
  interval_seconds: number | null
  daily_time: string | null
  timezone: string | null
  priority: 'background' | 'time_critical'
  enabled: boolean
  next_run_at?: string | null
  last_run_at?: string | null
}

/** One recorded execution of an agent. */
export interface AgentRun {
  id: string
  agent_id: string
  started_at: string | null
  finished_at: string | null
  status: 'completed' | 'failed' | 'skipped'
  trigger: string
  output: string | null
  error: string | null
  input_tokens: number | null
  output_tokens: number | null
  latency_ms: number | null
  /** Skill Hub skills injected into the run (NS-ADR-111). */
  skills?: { id: string; revision: string; review_status: string; chars: number }[]
  /** loaded | none | unavailable | unconfigured */
  skills_status?: string | null
}

/** `GET /api/agents/runner/status` — adaptive back-off state. */
export interface RunnerStatus {
  state: 'normal' | 'degraded' | 'backed_off'
  stretch: Record<string, number>
  throttles_in_window: number
  running: boolean
  skillhub_configured: boolean
}

// ─── Real API implementation ─────────────────────────────────────────────────

export const agentsApi = {
  /** `GET /agents` — native types + persisted instances (honest list). */
  list: async (): Promise<{ data: Agent[]; status: number }> => {
    const res = await apiFetch('/agents')
    if (!res.ok) throw await apiErrorFromResponse(res)
    const body = (await res.json().catch(() => null)) as
      | { data?: unknown[] }
      | unknown[]
      | null
    const data = Array.isArray(body)
      ? body
      : body && typeof body === 'object' && Array.isArray((body as { data: unknown[] }).data)
        ? (body as { data: Agent[] }).data
        : []
    return { data: data as Agent[], status: res.status }
  },

  /** `GET /agents/{id}` — detail; resolves native types AND persisted instances. */
  get: async (agentId: string): Promise<{ data: Agent; status: number }> => {
    const res = await apiFetch(`/agents/${agentId}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return { data: (await res.json()) as Agent, status: res.status }
  },

  /** `PATCH /api/agents/{id}` — partial update; 200 → updated row. */
  update: async (
    agentId: string,
    payload: AgentUpdatePayload,
  ): Promise<{ data: Agent; status: number }> => {
    const res = await apiFetch(`/api/agents/${agentId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return { data: (await res.json()) as Agent, status: res.status }
  },

  /**
   * `POST /api/agents` — create a durable agent instance; 201 → the new row.
   * A 409 duplicate name is rejected with `ApiError` like any other non-2xx.
   */
  create: async (
    payload: AgentCreatePayload,
  ): Promise<{ data: Agent; status: number }> => {
    const res = await apiFetch('/api/agents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return { data: (await res.json()) as Agent, status: res.status }
  },


  /** `DELETE /api/agents/{id}` — persisted instances only; built-ins are refused (405). */
  delete: async (agentId: string): Promise<void> => {
    const res = await apiFetch(`/api/agents/${agentId}`, { method: 'DELETE' })
    if (!res.ok) throw await apiErrorFromResponse(res)
  },

  /** `POST /api/agents/{id}/run` — run now; returns the recorded run. */
  run: async (agentId: string): Promise<AgentRun> => {
    const res = await apiFetch(`/api/agents/${agentId}/run`, { method: 'POST' })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as AgentRun
  },

  /** `GET /api/agents/{id}/runs` — newest first. */
  runs: async (agentId: string): Promise<AgentRun[]> => {
    const res = await apiFetch(`/api/agents/${agentId}/runs`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    const body = (await res.json()) as { data?: AgentRun[] }
    return body.data ?? []
  },

  getSchedule: async (agentId: string): Promise<AgentSchedule> => {
    const res = await apiFetch(`/api/agents/${agentId}/schedule`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as AgentSchedule
  },

  putSchedule: async (agentId: string, schedule: AgentSchedule): Promise<AgentSchedule> => {
    const res = await apiFetch(`/api/agents/${agentId}/schedule`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(schedule),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as AgentSchedule
  },

  runnerStatus: async (): Promise<RunnerStatus> => {
    const res = await apiFetch('/api/agents/runner/status')
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as RunnerStatus
  },
}
