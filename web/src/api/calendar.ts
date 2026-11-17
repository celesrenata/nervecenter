/**
 * Calendar account-connection API client.
 *
 * Typed client for the frozen `backend.api.endpoints.calendar` account
 * routes:
 *
 *   GET    /api/calendar/accounts                   → { data: row[], meta }
 *   POST   /api/calendar/accounts                   → create (201) / degraded
 *   GET    /api/calendar/accounts/{id}              → one account
 *   PATCH  /api/calendar/accounts/{id}              → field update
 *   DELETE /api/calendar/accounts/{id}              → remove (204)
 *   POST   /api/calendar/accounts/{id}/sync         → run one sync pass
 *   GET    /api/calendar/accounts/{id}/events       → { data: event[], meta }
 *
 * House degradation contract (Area 4): when the durable registry is not
 * configured the backend still answers 2xx, but with `executed: false` on
 * writes (create/update/sync) and an empty `data` list on reads. The UI must
 * treat `executed: false` as "not persisted" — never a success. Credentials
 * are written to the secrets vault only and are NEVER echoed back on the row
 * (at most a `secret_ref` pointer appears).
 */

import { apiFetch, apiErrorFromResponse, useMockMode } from './client'

// ─── Row + request shapes ────────────────────────────────────────────────────

export type CalendarProvider = 'google' | 'caldav' | 'apple'

/**
 * A connected calendar account (the `_account_dict` projection). The
 * credential is never present — only the opaque `secret_ref` pointer to the
 * vault secret, if one was stored.
 */
export interface CalendarAccountRow {
  id: string
  provider: CalendarProvider
  label: string | null
  account_ref: string | null
  /** Opaque pointer to the vault secret; `null` when no credential stored. */
  secret_ref: string | null
  timezone: string
  config: Record<string, unknown>
  enabled: boolean
  operational_status: string | null
  last_success_at: string | null
  last_attempt_at: string | null
  last_error_code: string | null
  last_error_message: string | null
  /** Last sync timestamp (from the durable `sync_state`), or null. */
  last_sync_at: string | null
  created_at: string | null
  updated_at: string | null
  /** Linked Gmail/Google connection id, when the account reuses its sign-in. */
  connection_id?: string | null
  /** True once the linked connection granted the `calendar_write` scope. */
  can_write?: boolean
}

/** Envelope returned by list endpoints (`GET /accounts`, `GET /…/events`). */
export interface CalendarListResponse {
  data: CalendarAccountRow[]
  meta: Record<string, unknown>
}

/** A durable event row (`_durable_event_dict` shape) for one account. */
export interface CalendarAccountEvent {
  event_id: string
  title: string
  start: string | null
  end: string | null
  all_day: boolean
  source: string
}

/** Body for `POST /api/calendar/accounts`. */
export interface CalendarCreatePayload {
  provider: CalendarProvider
  label?: string
  account_ref?: string
  timezone?: string
  config?: Record<string, unknown>
  /** Sent to the vault only; never echoed back on the row. */
  credential?: string
  /** Link to an existing Gmail connection; reuses its Google sign-in. */
  connection_id?: string
  enabled?: boolean
}

/** Body for `PATCH /api/calendar/accounts/{id}` (field-level update). */
export interface CalendarUpdatePayload {
  label?: string
  account_ref?: string
  timezone?: string
  config?: Record<string, unknown>
  credential?: string
  enabled?: boolean
}

/**
 * The honest 2xx "degraded write" the backend returns when the durable
 * registry is absent (`executed: false`). Distinguished from a real row by
 * `executed === false`.
 */
export interface CalendarDegradedWrite {
  accepted: boolean
  executed: false
  request_id: string
  timestamp: string
  detail: string
}

/** The outcome of a sync pass (runner summary, or the degraded write). */
export interface CalendarSyncResult {
  executed?: boolean
  detail?: string
  [key: string]: unknown
}

/** Type guard: is a create/update/sync body a degraded write, not a real row? */
export function isDegradedWrite(body: unknown): body is CalendarDegradedWrite {
  return (
    typeof body === 'object' &&
    body !== null &&
    (body as CalendarDegradedWrite).executed === false
  )
}

// ─── Real API implementation ─────────────────────────────────────────────────

export interface CalendarAccountsClient {
  list(): Promise<CalendarListResponse>
  create(payload: CalendarCreatePayload): Promise<CalendarAccountRow | CalendarDegradedWrite>
  get(id: string): Promise<CalendarAccountRow>
  update(id: string, payload: CalendarUpdatePayload): Promise<CalendarAccountRow | CalendarDegradedWrite>
  remove(id: string): Promise<void>
  sync(id: string): Promise<CalendarSyncResult>
  events(id: string): Promise<CalendarListResponse>
}

const idPath = (id: string) => `/api/calendar/accounts/${encodeURIComponent(id)}`

export const realCalendarAccountsApi: CalendarAccountsClient = {
  list: async () => {
    const res = await apiFetch('/api/calendar/accounts')
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as CalendarListResponse
  },
  create: async (payload) => {
    const res = await apiFetch('/api/calendar/accounts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as CalendarAccountRow | CalendarDegradedWrite
  },
  get: async (id) => {
    const res = await apiFetch(idPath(id))
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as CalendarAccountRow
  },
  update: async (id, payload) => {
    const res = await apiFetch(idPath(id), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as CalendarAccountRow | CalendarDegradedWrite
  },
  remove: async (id) => {
    const res = await apiFetch(idPath(id), { method: 'DELETE' })
    if (!res.ok) throw await apiErrorFromResponse(res)
  },
  sync: async (id) => {
    const res = await apiFetch(`${idPath(id)}/sync`, { method: 'POST' })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as CalendarSyncResult
  },
  events: async (id) => {
    const res = await apiFetch(`${idPath(id)}/events`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as CalendarListResponse
  },
}

// ─── Mock mode: honest empty reads + executed:false writes (Area 4) ───────────
//
// With no backend (`VITE_API_BASE` unset) there is nothing to connect to, so
// the client degrades honestly: empty lists and `executed: false` writes —
// never fabricated demo accounts (row 14).

const NO_STORAGE = 'calendar storage is not configured (no backend in mock mode)'

const degradedWrite = (): CalendarDegradedWrite => ({
  accepted: true,
  executed: false,
  request_id: 'mock',
  timestamp: new Date().toISOString(),
  detail: NO_STORAGE,
})

const EMPTY_LIST: CalendarListResponse = { data: [], meta: {} }

export const mockCalendarAccountsApi: CalendarAccountsClient = {
  list: async () => EMPTY_LIST,
  create: async () => degradedWrite(),
  get: async () => {
    throw new Error(NO_STORAGE)
  },
  update: async () => degradedWrite(),
  remove: async () => {
    /* nothing to remove — no backend */
  },
  sync: async () => ({ executed: false, detail: NO_STORAGE }),
  events: async () => EMPTY_LIST,
}

export const calendarAccountsApi = useMockMode
  ? mockCalendarAccountsApi
  : realCalendarAccountsApi

// ─── Ask calendar proposals (confirm-first) ──────────────────────────────────

export interface ProposalAccount {
  id: string
  label: string
  can_write: boolean
  connection_id?: string | null
}

export interface CalendarProposal {
  id: string
  title: string
  start: string
  end: string
  all_day?: boolean
  location?: string | null
  tz: string
  account: { id: string; label: string; can_write: boolean }
  accounts: ProposalAccount[]
  warnings: string[]
  assumptions: string[]
  conflicts?: Array<{ event_id: string; title: string; start: string; end: string }>
  expires_at?: string
  requires_confirmation?: boolean
  consent_required?: { code: string; message: string; connection_id?: string }
}

export interface ProposalConfirmEdits {
  account_id?: string
  title?: string
  start?: string
  end?: string
}

export interface ProposalConfirmResult {
  status: 'created'
  proposal_id: string
  event: { event_id: string; title: string; start: string; end: string; account_id?: string }
  undo?: { event_id: string; expires_at: string }
}

/** A refused calendar write, carrying the stable error code from the API. */
export class CalendarWriteError extends Error {
  status: number
  code: string
  connectionId: string | null

  constructor(status: number, code: string, message: string, connectionId: string | null = null) {
    super(message)
    this.name = 'CalendarWriteError'
    this.status = status
    this.code = code
    this.connectionId = connectionId
  }
}

async function writeErrorFromResponse(res: Response): Promise<CalendarWriteError> {
  let code = `http_${res.status}`
  let message = res.statusText || `HTTP ${res.status}`
  let connectionId: string | null = null
  try {
    const raw = (await res.json()) as Record<string, unknown>
    const inner = (raw.detail && typeof raw.detail === 'object' ? raw.detail : raw) as Record<string, unknown>
    const c = inner.code ?? inner.error_code
    if (typeof c === 'string') code = c
    if (typeof inner.message === 'string') message = inner.message
    else if (typeof raw.detail === 'string') message = raw.detail
    if (typeof inner.connection_id === 'string') connectionId = inner.connection_id
  } catch {
    /* non-JSON body */
  }
  return new CalendarWriteError(res.status, code, message, connectionId)
}

const jsonInit = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export async function confirmProposal(
  id: string,
  edits: ProposalConfirmEdits = {},
): Promise<ProposalConfirmResult> {
  const res = await apiFetch(`/api/calendar/proposals/${encodeURIComponent(id)}/confirm`, jsonInit(edits))
  if (!res.ok) throw await writeErrorFromResponse(res)
  return (await res.json()) as ProposalConfirmResult
}

export async function cancelProposal(id: string): Promise<void> {
  const res = await apiFetch(`/api/calendar/proposals/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
  if (!res.ok) throw await writeErrorFromResponse(res)
}

export async function undoEvent(eventId: string): Promise<void> {
  const res = await apiFetch(`/api/calendar/events/${encodeURIComponent(eventId)}/undo`, { method: 'POST' })
  if (!res.ok) throw await writeErrorFromResponse(res)
}
