/** Coding handoff API client (NS-ADR-106). */
import { apiFetch, apiErrorFromResponse } from './client'

export type HandoffStatus = 'queued' | 'claimed' | 'done' | 'failed' | 'cancelled'

export interface Handoff {
  id: string
  target: 'opencode' | 'menagerie'
  runner: 'central' | 'mac'
  title: string
  brief: string
  repo: string | null
  project_slug: string | null
  requested_by: string
  status: HandoffStatus
  push_approved: boolean
  claimed_by: string | null
  finished_at: string | null
  result_summary: string | null
  error: string | null
  created_at: string | null
}

export interface HandoffRunnerStatus {
  central: { configured: boolean; running: boolean; busy: boolean; url: string | null }
  mac: { pull_path_enabled: boolean; note: string }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

const post = (body?: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
})

const enc = encodeURIComponent

export const handoffsApi = {
  list: async () => (await request<{ handoffs: Handoff[] }>('/api/handoffs')).handoffs,
  status: () => request<HandoffRunnerStatus>('/api/handoffs/status'),
  create: (body: { target: string; title: string; brief: string; repo?: string; project_slug?: string }) =>
    request<Handoff>('/api/handoffs', post(body)),
  approvePush: (id: string) => request<Handoff>(`/api/handoffs/${enc(id)}/approve-push`, post()),
  cancel: (id: string) => request<Handoff>(`/api/handoffs/${enc(id)}/cancel`, post()),
  retry: (id: string) => request<Handoff>(`/api/handoffs/${enc(id)}/retry`, post()),
}
