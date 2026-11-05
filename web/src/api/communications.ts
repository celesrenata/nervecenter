/**
 * Communications search API client (R110 deterministic cross-channel search,
 * prompt2 §50 / P1-107).
 *
 * Typed client for the frozen search route:
 *
 *   GET /api/communications/search?q=<query>
 *       &person_id=<id>&channel=<channel>&within_week=<bool>
 *
 * Normalized envelope (the list contract, consistent with
 * `GET /api/communications`): the matched communications are returned under a
 * top-level `data` array in the canonical §7 `Communication` shape (metadata
 * + `content_ref` only — never raw bodies or tokens, §7). The search-specific
 * explainability extras live under `meta.search`:
 *
 *   meta.search.query            — the echoed free-text query
 *   meta.search.filters          — the deterministically parsed filters
 *   meta.search.matched_keywords — keywords found in the resolved text
 *   meta.search.request_groups   — per-person 'who asked me' groups
 *
 * `meta.pagination.total` carries the match count (== `data.length`).
 *
 * House degradation contract: in mock mode (no `VITE_API_BASE`) there is no
 * backend to search, so the client returns an honest empty result — never
 * fabricated matches (the §7 secret-free, honest-empty convention the other
 * api modules follow). The real client attaches the session bearer via
 * `apiFetch`; a 401 flows through the shared unauthorized handler (the
 * sign-in redirect), exactly like the other authorized clients.
 */

import { apiFetch, apiErrorFromResponse, useMockMode } from './client'

// ─── Wire types (exact service projections) ──────────────────────────────────

/**
 * One matched communication — the canonical §7 `Communication` shape the
 * list route also emits. Metadata + `content_ref` pointer only; no raw body.
 */
export interface CommunicationRow {
  communication_id: string
  person_id: string | null
  channel: string
  direction: string
  sender: string
  recipients: string[]
  timestamp: string
  thread_id: string | null
  content_ref: string
  attachments: string[]
  classification: Record<string, unknown>
  sensitivity: string
  content_routing?: Record<string, unknown>
  [key: string]: unknown
}

/** The deterministically parsed query filters (returned for explainability). */
export interface SearchFilters {
  within_week: boolean
  window_start: string | null
  window_end: string | null
  channel: string | null
  request_intent: boolean
  keywords: string[]
}

/** One 'who asked me' group — per person, with their request communications. */
export interface PersonRequestGroup {
  person_id: string
  display_name: string | null
  communication_ids: string[]
  first_request_at: string
  last_request_at: string
}

/** The `meta.search` sub-object carrying the search-specific extras. */
export interface SearchMeta {
  query: string
  filters: SearchFilters
  matched_keywords: string[]
  request_groups: PersonRequestGroup[]
}

export interface ApiResponseMeta {
  version?: string
  request_id?: string
  timestamp?: string
  pagination?: { [key: string]: number } | null
  search: SearchMeta
}

/** The normalized `/search` envelope: `data` rows + `meta` (with `.search`). */
export interface CommunicationSearchResponse {
  data: CommunicationRow[]
  meta: ApiResponseMeta
}

/** Optional scoping params for a search (names mirror the backend route). */
export interface CommunicationSearchParams {
  q: string
  personId?: string
  channel?: string
  withinWeek?: boolean
}

export interface CommunicationsSearchClient {
  search(params: CommunicationSearchParams): Promise<CommunicationSearchResponse>
}

// ─── Real API implementation ─────────────────────────────────────────────────

function buildQuery(params: CommunicationSearchParams): string {
  const qs = new URLSearchParams()
  qs.set('q', params.q)
  if (params.personId) qs.set('person_id', params.personId)
  if (params.channel) qs.set('channel', params.channel)
  // Only send within_week when explicitly set; the backend default is true.
  if (params.withinWeek !== undefined) qs.set('within_week', String(params.withinWeek))
  return qs.toString()
}

export const realCommunicationsApi: CommunicationsSearchClient = {
  search: async (params) => {
    const res = await apiFetch(`/api/communications/search?${buildQuery(params)}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as CommunicationSearchResponse
  },
}

// ─── Mock mode: honest empty result (§7 — never fabricate matches) ────────────
//
// With no backend (`VITE_API_BASE` unset) there is nothing to search, so the
// client degrades honestly: an empty `data` list with the parsed filters
// echoed under `meta.search` — never invented communications.

function emptyResult(params: CommunicationSearchParams): CommunicationSearchResponse {
  const withinWeek = params.withinWeek ?? true
  return {
    data: [],
    meta: {
      version: 'mock',
      request_id: `mock-${Math.random().toString(36).slice(2, 10)}`,
      timestamp: new Date().toISOString(),
      pagination: { total: 0 },
      search: {
        query: params.q,
        filters: {
          within_week: withinWeek,
          window_start: null,
          window_end: null,
          channel: params.channel ?? null,
          request_intent: false,
          keywords: [],
        },
        matched_keywords: [],
        request_groups: [],
      },
    },
  }
}

export const mockCommunicationsApi: CommunicationsSearchClient = {
  search: async (params) => emptyResult(params),
}

// ─── Exported client (mock-aware) ─────────────────────────────────────────────

export const communicationsApi = useMockMode
  ? mockCommunicationsApi
  : realCommunicationsApi
