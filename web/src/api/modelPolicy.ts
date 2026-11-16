/** Local-first model policy, OpenAI usage and escalation grants (NS-ADR-107). */
import { apiFetch, apiErrorFromResponse } from './client'

export interface UsageWindow {
  requests: number
  throttled: number
  errors: number
  input_tokens: number
  output_tokens: number
}

export interface Grant {
  subject_kind: 'agent' | 'project' | 'handoff'
  subject_id: string
  mode: 'owner_requested' | 'high_stakes'
}

export interface ModelPolicy {
  local_model: string
  max_local_failures: number
  openai: {
    key_configured: boolean
    model_id: string
    queue_depth: number
    in_flight: number
    next_slot_at: string | null
    today: UsageWindow
    last_hour: UsageWindow
    limits: Record<string, number>
  }
  grants: Grant[]
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (res.status === 204 ? undefined : await res.json()) as T
}

export const modelPolicyApi = {
  get: () => request<ModelPolicy>('/api/model-policy'),
  setGrant: (g: Grant) =>
    request<Grant>('/api/model-policy/grants', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(g),
    }),
  removeGrant: (kind: string, id: string) =>
    request<void>(`/api/model-policy/grants/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}
