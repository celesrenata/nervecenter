/** Long-term researcher API client (NS-ADR-103). */
import { apiFetch, apiErrorFromResponse } from './client'

export type ResearchStatus = 'planned' | 'running' | 'paused' | 'done' | 'failed'

export interface ResearchProject {
  id: string
  title: string
  question: string
  status: ResearchStatus
  budget_hours: number
  cadence_seconds: number
  combo: string
  spent_seconds: number
  error: string | null
  notes: string | null
  has_report: boolean
  next_step_at: string | null
  created_at: string | null
  finished_at: string | null
  steps_total?: number
  steps_done?: number
}

export interface ResearchStep {
  id: string
  ordinal: number
  goal: string
  status: 'pending' | 'running' | 'done' | 'skipped' | 'failed'
  result: string | null
  attempts: number
  finished_at: string | null
}

export interface ResearchDetail extends ResearchProject {
  steps: ResearchStep[]
  working_notes: string
  checkpoint_cursor: number
  checkpointed_at: string | null
}

export interface NewResearch {
  title: string
  question: string
  budget_hours: number
  cadence_seconds: number
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const researchApi = {
  list: async () => (await request<{ projects: ResearchProject[] }>('/api/research')).projects,
  detail: (id: string) => request<ResearchDetail>(`/api/research/${encodeURIComponent(id)}`),
  create: (body: NewResearch) => request<ResearchProject>('/api/research', json('POST', body)),
  pause: (id: string) => request<ResearchProject>(`/api/research/${encodeURIComponent(id)}/pause`, { method: 'POST' }),
  resume: (id: string) => request<ResearchProject>(`/api/research/${encodeURIComponent(id)}/resume`, { method: 'POST' }),
  saveNotes: (id: string, notes: string) =>
    request<ResearchProject>(`/api/research/${encodeURIComponent(id)}/notes`, json('PUT', { notes })),
  report: (id: string) =>
    request<{ id: string; status: ResearchStatus; report: string | null }>(`/api/research/${encodeURIComponent(id)}/report`),
}
