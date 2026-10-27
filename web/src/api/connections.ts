/**
 * Connection Center API client (spec `personal-ops-recovery` §3, workstream A).
 *
 * Typed client for workstream B's frozen `/api/connections` routes
 * (`backend/api/endpoints/connections.py`):
 *
 *   GET    /api/connections                       → list (200, empty when unwired)
 *   POST   /api/connections                        → create (201)
 *   GET    /api/connections/{id}                   → one connection (404)
 *   PATCH  /api/connections/{id}                   → update (200)
 *   DELETE /api/connections/{id}                   → remove + purge secrets (204)
 *   POST   /api/connections/{id}/auth              → start OAuth/pairing flow (200)
 *   POST   /api/connections/{id}/verify            → run login probe (200)
 *   POST   /api/connections/{id}/sync              → trigger sync (200)
 *   POST   /api/connections/{id}/import            → initial-history import (200)
 *   POST   /api/connections/{id}/reconnect         → re-verify + re-sync (200)
 *   GET    /api/connections/{id}/diagnostics       → actionable fix (200)
 *   GET    /api/connections/{id}/permissions       → editable surface (200)
 *   PATCH  /api/connections/{id}/permissions       → allow-listed config (200)
 *
 * House degradation semantics (Area 4) are mirrored in mock mode: reads
 * return an honest empty list, writes return `executed: false` — never
 * fabricated demo connections (row 14: the UI works without sample
 * records).
 */

import { apiFetch, apiErrorFromResponse, useMockMode } from './client'

// ─── Frozen state vocabulary (mirrors `backend.connectors.registry`) ─────────

/** The five frozen UI states derived from the two orthogonal axes. */
export type ConnectionState =
  | 'connected'
  | 'syncing'
  | 'degraded'
  | 'auth_required'
  | 'not_configured'

export type AuthStatus =
  | 'unconfigured'
  | 'pairing_pending'
  | 'authenticated'
  | 'auth_expired'
  | 'auth_failed'

export type OperationalStatus = 'never_synced' | 'syncing' | 'healthy' | 'degraded' | 'error'

/** Connection kinds the control plane manages (`CONNECTION_KINDS`). */
export type ConnectionKind = 'gmail' | 'imap' | 'graph_mail' | 'relay_device' | 'calendar'

// ─── Row + request shapes ────────────────────────────────────────────────────

/**
 * A connection row as returned by every B endpoint (the `_public(row)`
 * projection). Both status axes are ALWAYS present and independent: the UI
 * must render them as two separate badges. `state` is the frozen five-state
 * derivation; a login probe that passed but never ingested leaves
 * `operational_status === 'never_synced'` — the row must NOT render as
 * operational.
 */
export interface Connection {
  id: string
  kind: string
  label: string | null
  enabled: boolean
  config: Record<string, unknown>
  /** Opaque pointer to the stored secret kind; never the secret itself. */
  secret_ref: string | null
  auth_status: AuthStatus
  operational_status: OperationalStatus
  state: ConnectionState
  last_success_at: string | null
  last_attempt_at: string | null
  last_error_code: string | null
  last_error_message: string | null
  import_progress: Record<string, unknown>
  created_at: string | null
  updated_at: string | null
}

export interface ConnectionCreateInput {
  kind: string
  label?: string | null
  /** Non-secret config only — credentials are stored via the vault flows. */
  config?: Record<string, unknown>
  enabled?: boolean
}

export interface ConnectionUpdatePatch {
  label?: string | null
  config?: Record<string, unknown>
  enabled?: boolean | null
}

/** `POST /{id}/auth` — OAuth start, relay pairing pointer, or completed exchange. */
export type AuthFlowResult =
  | { authorization_url: string; state: string; provider: string }
  | { ok: true; detail: string; pair_endpoint: string; auth_status: AuthStatus }
  | ({ ok: true; detail: string } & Connection)
  | DegradedWrite

/** `POST /{id}/verify` — probe outcome; `ok:false` carries the stable error code. */
export type VerifyResult =
  | ({ ok: true; detail: string } & Connection)
  | ({ ok: false; error_code: string; error_message?: string } & Connection)
  | DegradedWrite

/** `POST /{id}/sync` | `/import` | `/reconnect` outcome. */
export type SyncResult =
  | ({ ok: true; import_job_id?: string; reconnect?: boolean } & Connection)
  | ({ ok: false; error_code: string; reconnect?: boolean } & Connection)
  | DegradedWrite

/** `GET /{id}/diagnostics` — plain-language fix + job state (acceptance row 13). */
export interface ConnectionDiagnostics extends Connection {
  diagnosis: {
    error_code: string | null
    fix: string
    last_attempt_at: string | null
    last_success_at: string | null
    import_job: {
      id: string
      kind: string
      state: string
      progress: Record<string, unknown>
      error_code: string | null
      started_at: string | null
      finished_at: string | null
    } | null
  }
}

/** `GET /{id}/permissions` — editable surface for the kind. */
export interface ConnectionPermissions {
  kind: string
  editable_keys: string[]
  denied_keys: string[]
}

/** `PATCH /{id}/permissions` outcome (allow-listed keys applied, rest ignored). */
export type PermissionPatchResult =
  | ({ ok: true; applied_keys: string[]; ignored_keys: string[] } & Connection)
  | DegradedWrite

/** `POST /api/relay/pair` — one-time pairing token (returned once). */
export interface RelayPairResult {
  device_id: string
  token: string
  connection_id: string
}

/** Honest 202 write degradation when durable storage is absent. */
export interface DegradedWrite {
  accepted: true
  executed: false
  request_id: string
  timestamp: string
  detail: string
}

export function isDegradedWrite(result: unknown): result is DegradedWrite {
  return (
    typeof result === 'object' &&
    result !== null &&
    (result as DegradedWrite).executed === false
  )
}

// ─── Real API implementation ─────────────────────────────────────────────────

async function jsonPost(path: string, body?: unknown): Promise<unknown> {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw await apiErrorFromResponse(res)
  return res.json()
}

async function jsonPatch(path: string, body?: unknown): Promise<unknown> {
  const res = await apiFetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw await apiErrorFromResponse(res)
  return res.json()
}

export const realConnectionsApi = {
  list: async (): Promise<{ data: Connection[]; status: number }> => {
    const res = await apiFetch('/api/connections')
    if (!res.ok) throw await apiErrorFromResponse(res)
    const body = (await res.json()) as { data?: Connection[] }
    return { data: body.data ?? [], status: res.status }
  },

  get: async (id: string): Promise<Connection> => {
    const res = await apiFetch(`/api/connections/${encodeURIComponent(id)}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as Connection
  },

  create: async (input: ConnectionCreateInput): Promise<Connection | DegradedWrite> => {
    // 201 → created row; degraded 202 → executed:false shape (Area 4).
    return (await jsonPost('/api/connections', input)) as Connection | DegradedWrite
  },

  update: async (id: string, patch: ConnectionUpdatePatch): Promise<Connection | DegradedWrite> => {
    return (await jsonPatch(
      `/api/connections/${encodeURIComponent(id)}`,
      patch,
    )) as Connection | DegradedWrite
  },

  remove: async (id: string): Promise<void> => {
    const res = await apiFetch(`/api/connections/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (!res.ok && res.status !== 204) throw await apiErrorFromResponse(res)
  },

  startAuth: async (id: string, body?: { code?: string; state?: string }): Promise<AuthFlowResult> => {
    return (await jsonPost(`/api/connections/${encodeURIComponent(id)}/auth`, body ?? {})) as AuthFlowResult
  },

  verify: async (id: string): Promise<VerifyResult> => {
    return (await jsonPost(`/api/connections/${encodeURIComponent(id)}/verify`)) as VerifyResult
  },

  syncNow: async (id: string): Promise<SyncResult> => {
    return (await jsonPost(`/api/connections/${encodeURIComponent(id)}/sync`)) as SyncResult
  },

  startImport: async (id: string): Promise<SyncResult> => {
    return (await jsonPost(`/api/connections/${encodeURIComponent(id)}/import`)) as SyncResult
  },

  reconnect: async (id: string): Promise<SyncResult> => {
    return (await jsonPost(`/api/connections/${encodeURIComponent(id)}/reconnect`)) as SyncResult
  },

  getDiagnostics: async (id: string): Promise<ConnectionDiagnostics> => {
    const res = await apiFetch(`/api/connections/${encodeURIComponent(id)}/diagnostics`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as ConnectionDiagnostics
  },

  getPermissions: async (id: string): Promise<ConnectionPermissions> => {
    const res = await apiFetch(`/api/connections/${encodeURIComponent(id)}/permissions`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as ConnectionPermissions
  },

  updatePermissions: async (id: string, config: Record<string, unknown>): Promise<PermissionPatchResult> => {
    return (await jsonPatch(
      `/api/connections/${encodeURIComponent(id)}/permissions`,
      { config },
    )) as PermissionPatchResult
  },

  /** `POST /api/relay/pair` — mint a one-time pairing token (frozen relay route). */
  relayPair: async (deviceName?: string | null): Promise<RelayPairResult | DegradedWrite> => {
    return (await jsonPost('/api/relay/pair', deviceName ? { device_name: deviceName } : {})) as
      | RelayPairResult
      | DegradedWrite
  },
}

// ─── Mock mode: honest empty list + executed:false writes (Area 4) ──────────
//
// When `VITE_API_BASE` is unset there is no backend at all, so the client
// must not fabricate demo connections (row 14). Reads degrade to the honest
// empty list and writes to the honest `executed: false` shape — the same
// contract the backend itself answers when its durable registry is absent.

const NO_STORAGE = 'durable storage is not configured (no backend in mock mode)'

const mockConnectionsApi = {
  list: async (): Promise<{ data: Connection[]; status: number }> => ({ data: [], status: 200 }),
  get: async (id: string): Promise<Connection> => {
    // House read degradation: unknown id → synthetic representation.
    return { id, resource: 'connection' } as unknown as Connection
  },
  create: async (): Promise<Connection | DegradedWrite> => degradedWrite(NO_STORAGE),
  update: async (): Promise<Connection | DegradedWrite> => degradedWrite(NO_STORAGE),
  remove: async (): Promise<void> => undefined,
  startAuth: async (): Promise<AuthFlowResult> => degradedWrite(NO_STORAGE),
  verify: async (): Promise<VerifyResult> => degradedWrite(NO_STORAGE),
  syncNow: async (): Promise<SyncResult> => degradedWrite(NO_STORAGE),
  startImport: async (): Promise<SyncResult> => degradedWrite(NO_STORAGE),
  reconnect: async (): Promise<SyncResult> => degradedWrite(NO_STORAGE),
  getDiagnostics: async (): Promise<ConnectionDiagnostics> => {
    throw new Error(NO_STORAGE)
  },
  getPermissions: async (): Promise<ConnectionPermissions> => {
    throw new Error(NO_STORAGE)
  },
  updatePermissions: async (): Promise<PermissionPatchResult> => degradedWrite(NO_STORAGE),
  relayPair: async (): Promise<RelayPairResult | DegradedWrite> => degradedWrite(NO_STORAGE),
}

function degradedWrite(detail: string): DegradedWrite {
  return {
    accepted: true,
    executed: false,
    request_id: 'req-local',
    timestamp: new Date().toISOString(),
    detail,
  }
}

// ─── Exported client (mock-aware, mirrors the `emailApi` style) ─────────────

export const connectionsApi = useMockMode ? mockConnectionsApi : realConnectionsApi

/**
 * Convenience wrapper the views call — the frozen route list, one method
 * per route. Views consume this; `realConnectionsApi` is exported for tests.
 */
export const connections = {
  listConnections: connectionsApi.list,
  getConnection: connectionsApi.get,
  createConnection: connectionsApi.create,
  updateConnection: connectionsApi.update,
  removeConnection: connectionsApi.remove,
  startAuth: connectionsApi.startAuth,
  verifyConnection: connectionsApi.verify,
  syncNow: connectionsApi.syncNow,
  startImport: connectionsApi.startImport,
  reconnect: connectionsApi.reconnect,
  getDiagnostics: connectionsApi.getDiagnostics,
  getPermissions: connectionsApi.getPermissions,
  updatePermissions: connectionsApi.updatePermissions,
  relayPair: connectionsApi.relayPair,
}
