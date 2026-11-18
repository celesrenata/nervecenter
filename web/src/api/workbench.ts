/** Workbench API client (opencode-menagerie-workbench, Phase 1). */
import { apiFetch, apiErrorFromResponse } from './client'

export type WorkbenchKind = 'opencode' | 'menagerie'

export interface WorkbenchConfig {
  phase: number
  ide_enabled: boolean
  /** https URL of the opencode web UI host, or null when not configured. */
  opencode_web_url?: string | null
  default_models?: Record<string, string>
  [key: string]: unknown
}

export interface WorkbenchStoreHealth {
  mounted: boolean
  writable: boolean
  free_bytes: number | null
}

export type WorkbenchHealth = Record<string, WorkbenchStoreHealth>

export interface WorkbenchProject {
  kind: WorkbenchKind
  slug: string
  archived?: boolean
  push_approved?: boolean
  last_activity_at?: string | null
  [key: string]: unknown
}

export interface WorkbenchProjectDetail extends WorkbenchProject {
  sessions?: unknown[]
  handoffs?: unknown[]
}

export interface WorkbenchProjectCreate {
  kind: WorkbenchKind
  slug: string
  git_url?: string
  [key: string]: unknown
}

export interface WorkbenchUnregistered {
  kind: WorkbenchKind
  name: string
  [key: string]: unknown
}

/** Sent on every state-changing request; the API refuses these without it (Req 6.10). */
const WORKBENCH_HEADER = { 'X-NC-Workbench': '1' }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

const write = (method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', body?: unknown): RequestInit => ({
  method,
  headers: {
    ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    ...WORKBENCH_HEADER,
  },
  body: body === undefined ? undefined : JSON.stringify(body),
})

const enc = encodeURIComponent
const base = '/api/workbench'
const projectPath = (kind: string, slug: string) => `${base}/projects/${enc(kind)}/${enc(slug)}`

export const workbenchApi = {
  config: () => request<WorkbenchConfig>(`${base}/config`),
  health: () => request<WorkbenchHealth>(`${base}/health`),
  listProjects: () => request<{ projects: WorkbenchProject[] }>(`${base}/projects`),
  createProject: (body: WorkbenchProjectCreate) =>
    request<WorkbenchProject>(`${base}/projects`, write('POST', body)),
  getProject: (kind: string, slug: string) =>
    request<WorkbenchProjectDetail>(projectPath(kind, slug)),
  archiveProject: (kind: string, slug: string) =>
    request<WorkbenchProject>(`${projectPath(kind, slug)}/archive`, write('POST')),
  deleteProject: (kind: string, slug: string, confirm: string) =>
    request<unknown>(projectPath(kind, slug), write('DELETE', { confirm })),
  listUnregistered: () => request<{ unregistered: WorkbenchUnregistered[] }>(`${base}/unregistered`),
  importProject: (body: { kind: WorkbenchKind; name: string; [key: string]: unknown }) =>
    request<WorkbenchProject>(`${base}/projects/import`, write('POST', body)),
  setPushApproval: (kind: string, slug: string, approved: boolean) =>
    request<WorkbenchProject>(`${projectPath(kind, slug)}/push-approval`, write('POST', { approved })),

  // ---- Phase 2: files (opencode projects only; no `kind` in the path) ----
  listFiles: (slug: string, path = '') =>
    request<{ path: string; entries: WorkbenchFileEntry[] }>(`${editorPath(slug)}/files?path=${enc(path)}`),
  readFile: (slug: string, path: string) =>
    request<WorkbenchFile>(`${editorPath(slug)}/file?path=${enc(path)}`),
  /** Save with optimistic concurrency; a stale `base_mtime_ns` is rejected by the API (Req 4.5). */
  saveFile: (slug: string, body: { path: string; content: string; base_mtime_ns: number }) =>
    request<WorkbenchFileSaved>(`${editorPath(slug)}/file`, write('PUT', body)),
  fsOperation: (slug: string, op: WorkbenchFsOperation) =>
    request<unknown>(`${editorPath(slug)}/fs`, write('POST', op)),
  changes: (slug: string, since?: string) =>
    request<WorkbenchChanges>(`${editorPath(slug)}/changes${since ? `?since=${enc(since)}` : ''}`),

  // ---- Phase 2: Sessions ----
  createSession: (slug: string, body: WorkbenchSessionCreate) =>
    request<WorkbenchSession>(`${editorPath(slug)}/sessions`, write('POST', body)),
  listSessions: (slug: string) =>
    request<{ sessions: WorkbenchSession[] }>(`${editorPath(slug)}/sessions`),
  listUsage: (sessionId?: string, limit?: number) => {
    const q = new URLSearchParams()
    if (sessionId) q.set('session_id', sessionId)
    if (limit) q.set('limit', String(limit))
    const qs = q.toString()
    return request<{ usage: WorkbenchUsage[] }>(`${base}/usage${qs ? `?${qs}` : ''}`)
  },
  sendMessage: (id: string, text: string) =>
    request<unknown>(`${sessionPath(id)}/messages`, write('POST', { text })),
  abortSession: (id: string) => request<unknown>(`${sessionPath(id)}/abort`, write('POST')),
  selectionAction: (id: string, body: WorkbenchSelectionAction) =>
    request<unknown>(`${sessionPath(id)}/selection-action`, write('POST', body)),

  // ---- Phase 3: Browser_IDE ----
  listIdeInstances: () => request<{ instances: WorkbenchIdeInstance[] }>(`${base}/ide`),
  startIde: (kind: string, slug: string) =>
    request<WorkbenchIdeInstance>(`${idePath(kind, slug)}/start`, write('POST')),
  /** Sets the path-scoped nc_ide cookie the IDE iframe authenticates with. */
  ideTicket: async (kind: string, slug: string): Promise<void> => {
    const res = await apiFetch(`${idePath(kind, slug)}/ticket`, write('POST'))
    if (!res.ok) throw await apiErrorFromResponse(res)
  },
  stopIde: (kind: string, slug: string) =>
    request<WorkbenchIdeInstance>(`${idePath(kind, slug)}/stop`, write('POST')),
  /** Menagerie task status for a running IDE_Instance (Req 3.11). */
  ideTaskStatus: (kind: string, slug: string) =>
    request<WorkbenchIdeTaskStatus>(`${idePath(kind, slug)}/status`),
}

const idePath = (kind: string, slug: string) => `${base}/ide/${enc(kind)}/${enc(slug)}`

export type WorkbenchIdeState = 'starting' | 'running' | 'stopping' | 'stopped' | 'failed' | 'pending'

export interface WorkbenchIdeInstance {
  kind: WorkbenchKind
  slug: string
  state: WorkbenchIdeState | string
  url?: string
  started_at?: string | null
  last_traffic_at?: string | null
  reason?: string | null
  [key: string]: unknown
}

export interface WorkbenchIdeTaskStatus {
  kind: WorkbenchKind
  slug: string
  state: string
  running?: boolean
  task?: string | null
  detail?: string | null
  updated_at?: string | null
  [key: string]: unknown
}

const editorPath = (slug: string) => `${base}/projects/${enc(slug)}`
const sessionPath = (id: string) => `${base}/sessions/${enc(id)}`

export interface WorkbenchFileEntry {
  name: string
  path: string
  type: 'file' | 'dir' | string
  size?: number
  mtime_ns?: number
  [key: string]: unknown
}
export interface WorkbenchFile {
  path: string
  content?: string
  mtime_ns: number
  size: number
  type: string
  [key: string]: unknown
}
export interface WorkbenchFileSaved {
  path: string
  mtime_ns: number
  size?: number
  [key: string]: unknown
}
export type WorkbenchFsOperation =
  | { op: 'create'; path: string; type?: 'file' | 'dir' }
  | { op: 'rename'; path: string; new_name: string }
  | { op: 'move'; path: string; dest: string }
  | { op: 'delete'; path: string }
export interface WorkbenchChanges {
  token?: string
  paths: string[]
  [key: string]: unknown
}
export interface WorkbenchSessionCreate {
  model?: string
  confirm_nonlocal?: boolean
  [key: string]: unknown
}
export interface WorkbenchSession {
  id: string
  model?: string
  status?: string
  [key: string]: unknown
}
export interface WorkbenchUsage {
  id: number
  session_id: string
  model: string
  input_tokens: number
  output_tokens: number
  recorded_at: string
}
export interface WorkbenchSelectionAction {
  path: string
  selection: string
  instruction: string
  [key: string]: unknown
}

/** One SSE event from `GET /api/workbench/sessions/{id}/events`. */
export interface WorkbenchSessionEvent {
  event: string
  data: unknown
}

/** Parse one SSE frame; unlike askStream, any named event is passed through. */
export function parseWorkbenchSseFrame(frame: string): WorkbenchSessionEvent | null {
  let name = 'message'
  const dataLines: string[] = []
  for (const rawLine of frame.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) name = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (dataLines.length === 0) return null
  const raw = dataLines.join('\n')
  try {
    return { event: name, data: JSON.parse(raw) }
  } catch {
    return { event: name, data: raw }
  }
}

/** Stream Session events (GET, SSE) until the server closes or `signal` aborts. */
export async function streamSessionEvents(
  id: string,
  onEvent: (event: WorkbenchSessionEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await apiFetch(`${sessionPath(id)}/events`, {
    headers: { Accept: 'text/event-stream' },
    signal,
  })
  if (!res.ok) throw await apiErrorFromResponse(res)
  if (!res.body) throw new Error('The server answered without a stream body')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const drain = (final: boolean) => {
    buffer = buffer.replace(/\r\n/g, '\n')
    let idx = buffer.indexOf('\n\n')
    while (idx !== -1) {
      const ev = parseWorkbenchSseFrame(buffer.slice(0, idx))
      buffer = buffer.slice(idx + 2)
      if (ev) onEvent(ev)
      idx = buffer.indexOf('\n\n')
    }
    if (final && buffer.trim().length > 0) {
      const ev = parseWorkbenchSseFrame(buffer)
      buffer = ''
      if (ev) onEvent(ev)
    }
  }
  for (;;) {
    const { value, done } = await reader.read()
    if (value) buffer += decoder.decode(value, { stream: !done })
    drain(done)
    if (done) break
  }
}
