/**
 * Infrastructure connectors and change plans (NS-ADR-120). Credentials are
 * write-only: responses only say whether one is set.
 */
import { apiFetch, apiErrorFromResponse } from './client'

export type InfraKind = 'kubernetes' | 'ssh'

export interface InfraConnector {
  id: string
  kind: InfraKind
  name: string
  secret_set: boolean
  updated_at: string | null
  server?: string
  namespace?: string | null
  ca_set?: boolean
  insecure_skip_tls_verify?: boolean
  host?: string
  port?: number
  user?: string
  host_key_status?: 'unpinned' | 'pending' | 'pinned'
  fingerprints?: string[]
  pending_fingerprints?: string[]
}

export interface InfraStep {
  connector: string
  purpose?: string
  args?: string[]
  stdin?: string
  command?: string
  params?: Record<string, unknown>
}

export interface InfraResult {
  index: number
  ok: boolean
  exit: number | null
  output: string
}

export interface InfraPlan {
  id: string
  agent_id: string | null
  title: string
  summary: string
  steps: InfraStep[]
  verify: InfraStep[]
  rollback: string
  status: 'proposed' | 'approved' | 'rejected' | 'running' | 'done' | 'failed'
  act_mode: 'opencode' | 'direct'
  decided_by: string | null
  decided_at: string | null
  results: InfraResult[]
  handoff_id: string | null
  created_at: string | null
  /** Output so far of the step running now (redacted); null when none is running. */
  live?: { index: number; started_at: string; output: string } | null
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

const post = (path: string, body?: unknown, method = 'POST') =>
  apiFetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })

export const infraApi = {
  connectors: async () =>
    (await json<{ connectors: InfraConnector[] }>(await apiFetch('/api/infra/connectors'))).connectors,
  create: async (kind: InfraKind, name: string, config: Record<string, unknown>) =>
    json<InfraConnector>(await post('/api/infra/connectors', { kind, name, config })),
  setSecret: async (name: string, secret: Record<string, string>) =>
    json<InfraConnector>(await post(`/api/infra/connectors/${encodeURIComponent(name)}/secret`, secret, 'PUT')),
  remove: async (name: string) => {
    const res = await apiFetch(`/api/infra/connectors/${encodeURIComponent(name)}`, { method: 'DELETE' })
    if (!res.ok) throw await apiErrorFromResponse(res)
  },
  scanHostKey: async (name: string) =>
    json<InfraConnector>(await post(`/api/infra/connectors/${encodeURIComponent(name)}/scan-host-key`)),
  trustHostKey: async (name: string, fingerprints: string[]) =>
    json<InfraConnector>(
      await post(`/api/infra/connectors/${encodeURIComponent(name)}/trust-host-key`, { fingerprints }),
    ),
  test: async (name: string) =>
    json<{ ok: boolean; output?: string; denied_reason?: string }>(
      await post(`/api/infra/connectors/${encodeURIComponent(name)}/test`),
    ),
  plans: async () => (await json<{ plans: InfraPlan[] }>(await apiFetch('/api/infra/plans'))).plans,
  approve: async (id: string, act_mode: 'opencode' | 'direct') =>
    json<InfraPlan>(await post(`/api/infra/plans/${id}/approve`, { act_mode })),
  reject: async (id: string) => json<InfraPlan>(await post(`/api/infra/plans/${id}/reject`)),
  verify: async (id: string) =>
    json<{ verify: (InfraResult & { denied_reason?: string })[] }>(await post(`/api/infra/plans/${id}/verify`)),
}
