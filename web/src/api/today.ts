/**
 * Today landing-page API client (spec §13, workstream L).
 *
 * Typed client for the frozen `backend.api.endpoints.today` routes:
 *
 *   GET   /api/today      → { needs_you, coming_up, handled, low_energy,
 *                             simplified, generated_at }
 *   PATCH /api/today/mode → persist Low Energy Mode ({ low_energy: bool })
 *
 * Item shapes are the exact projections L's providers emit (durable
 * `attention_items` / `commitments` / `calendar_events` rows):
 *
 *   needs_you item — { id, kind, title, body, importance, urgency,
 *                      deadline_at, source_refs, recommended_action }
 *   coming_up item — { id, kind, title, deadline_at }
 *   handled item   — { id, kind, title, state, at }
 *
 * There are deliberately NO streak or "missed" fields in this shape (spec §13:
 * Low Energy Mode must never punish).
 *
 * House degradation semantics (Area 4): with no durable registry the backend
 * answers 200 with empty sections (`low_energy: false`, `simplified: false`)
 * and the mode write answers 200 with `executed: false` + `low_energy: false`.
 * Mock mode mirrors that: reads return an honest empty response, the mode
 * write reports `low_energy: false` — nothing is persisted, nothing is a
 * fabricated success.
 */

import { apiFetch, apiErrorFromResponse, useMockMode } from './client'

/** urgency vocabulary (frozen — mirrors the durable column's values). */
export type TodayUrgency = 'low' | 'normal' | 'high' | 'critical'

/** A `needs_you` item: a durable attention item or open commitment. */
export interface TodayNeedsYouItem {
  id: string
  kind: 'attention' | 'commitment'
  title: string
  body: string | null
  /** 1..10 for attention items; null for commitments (no column there). */
  importance: number | null
  /** One of `TodayUrgency`; null for commitments. */
  urgency: TodayUrgency | null
  deadline_at: string | null
  source_refs: string[]
  recommended_action: string | null
}

/** A `coming_up` item: appointment / deadline in the next 48 hours.
 * `routine` is contributed by the routines provider (workstream H) — the
 * view renders it generically (title + deadline). */
export interface TodayComingUpItem {
  id: string
  kind: 'attention' | 'commitment' | 'calendar' | 'routine'
  title: string
  deadline_at: string | null
}

/** A `handled` item: resolved/dismissed/snoozed or completed in the last 24h.
 * `routine` is contributed by the routines provider (workstream H); its
 * payload has no `state` field (the view tolerates the undefined label). */
export interface TodayHandledItem {
  id: string
  kind: 'attention' | 'commitment' | 'routine'
  title: string
  state: string
  at: string
}

/**
 * `PATCH /api/today/mode` — degraded write contract (Area 4): when no durable
 * registry exists the backend answers `{accepted, executed:false,
 * low_energy:false, detail}`; a durable write returns `{low_energy: bool}`.
 */
export interface SetLowEnergyModeResult {
  low_energy: boolean
  executed?: boolean
  detail?: string
}

/** `GET /api/today` — the frozen response shape (no streak fields by design). */
export interface TodayResponse {
  needs_you: TodayNeedsYouItem[]
  coming_up: TodayComingUpItem[]
  handled: TodayHandledItem[]
  low_energy: boolean
  /** True while Low Energy Mode is on (render the reduced layout). */
  simplified: boolean
  generated_at: string
}

// ─── Real API implementation ─────────────────────────────────────────────────

export const realTodayApi = {
  getToday: async (): Promise<TodayResponse> => {
    const res = await apiFetch('/api/today')
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as TodayResponse
  },

  setLowEnergyMode: async (on: boolean): Promise<SetLowEnergyModeResult> => {
    const res = await apiFetch('/api/today/mode', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ low_energy: on }),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    const body = (await res.json()) as SetLowEnergyModeResult
    return {
      low_energy: body.low_energy,
      executed: body.executed,
      detail: body.detail,
    }
  },
}

// ─── Mock implementation (honest degradation — no demo data, row 14) ─────────

const emptyToday = (): TodayResponse => ({
  needs_you: [],
  coming_up: [],
  handled: [],
  low_energy: false,
  simplified: false,
  generated_at: new Date().toISOString(),
})

export const mockTodayApi = {
  getToday: async (): Promise<TodayResponse> => emptyToday(),

  // No durable store in mock mode — the write is honest: not persisted,
  // reported as off (the view surfaces a note instead of faking success).
  setLowEnergyMode: async (): Promise<SetLowEnergyModeResult> => ({
    low_energy: false,
    executed: false,
    detail: 'durable storage is not configured',
  }),
}

// ─── Exported client (mock-aware, mirrors the `connectionsApi` style) ────────

export const todayApi = useMockMode ? mockTodayApi : realTodayApi
