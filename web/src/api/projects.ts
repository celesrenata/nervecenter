/** Shared project context API client (NS-ADR-104). */
import { apiFetch, apiErrorFromResponse } from './client'

export interface ProjectSummary {
  slug: string
  title: string
  summary: string
  note_count?: number
  updated_at: string | null
}

export interface ProjectNote {
  id: string
  author: string
  body: string
  created_at: string | null
}

export interface ProjectDetail extends ProjectSummary {
  notes: ProjectNote[]
}

export interface OpenAIStatus {
  provider: string
  model_id: string
  key_configured: boolean
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

const enc = encodeURIComponent

export const projectsApi = {
  list: async () => (await request<{ projects: ProjectSummary[] }>('/api/projects')).projects,
  detail: (slug: string) => request<ProjectDetail>(`/api/projects/${enc(slug)}`),
  create: (body: { slug: string; title: string; summary: string }) =>
    request<ProjectSummary>('/api/projects', json('POST', body)),
  saveSummary: (slug: string, summary: string) =>
    request<ProjectSummary>(`/api/projects/${enc(slug)}`, json('PATCH', { summary })),
  addNote: (slug: string, body: string) =>
    request<ProjectNote>(`/api/projects/${enc(slug)}/notes`, json('POST', { body })),
  openaiStatus: () => request<OpenAIStatus>('/api/projects/openai/status'),
  ask: (slug: string, prompt: string) =>
    request<{ content: string; model: string }>(`/api/projects/${enc(slug)}/ask`, json('POST', { prompt })),
}
