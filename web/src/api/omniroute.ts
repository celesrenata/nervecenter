/**
 * OmniRoute key management client (NS-ADR-118). Write-only keys: no call ever
 * returns a key value, only set/not-set, source and last-changed time.
 */
import { apiFetch, apiErrorFromResponse } from './client'

export type OmniRouteKeyKind = 'regular' | 'privileged'

export interface OmniRouteKeyStatus {
  configured: boolean
  /** `vault` (set in Settings), `env` (cluster Secret) or `none`. */
  source: 'vault' | 'env' | 'none'
  vault_set: boolean
  changed_at: string | null
}

export interface OmniRouteAuditEntry {
  at: string | null
  actor: string
  key: OmniRouteKeyKind
  action: string
  outcome: string
}

export interface OmniRouteSettings {
  keys: Record<OmniRouteKeyKind, OmniRouteKeyStatus>
  vault_writable: boolean
  audit: OmniRouteAuditEntry[]
}

export interface OmniRouteCheck {
  ok: boolean
  status: string
  routes?: number
  ready?: number
  skills?: number
}

export interface OmniRouteTestResult {
  key: OmniRouteKeyKind
  ok: boolean
  inference?: OmniRouteCheck
  catalog?: OmniRouteCheck
  mcp?: OmniRouteCheck
}

const BASE = '/api/settings/omniroute'

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

export const omnirouteApi = {
  get: async (): Promise<OmniRouteSettings> => json(await apiFetch(BASE)),
  setKey: async (kind: OmniRouteKeyKind, value: string): Promise<OmniRouteSettings> =>
    json(
      await apiFetch(`${BASE}/keys/${kind}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value }),
      }),
    ),
  clearKey: async (kind: OmniRouteKeyKind): Promise<OmniRouteSettings> =>
    json(await apiFetch(`${BASE}/keys/${kind}`, { method: 'DELETE' })),
  testKey: async (kind: OmniRouteKeyKind): Promise<OmniRouteTestResult> =>
    json(await apiFetch(`${BASE}/keys/${kind}/test`, { method: 'POST' })),
}
