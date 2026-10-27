/**
 * Routines API client (Personal-Ops Recovery §9 — workstream H).
 *
 * Personal routines (wake, workday prep, bedtime, …) with explicit
 * check-ins. **Hard rule: no streaks, no guilt, no asleep/awave
 * assertions.** A missed window is a single gentle note; missing sleep
 * data keeps regularity at "unknown". This client only talks to the
 * local NerveCenter API.
 */

import { apiFetch, useMockMode } from './client'

export const ROUTINE_KINDS = [
  'wake',
  'workday_prep',
  'bedtime',
  'morning_briefing',
  'appointment_prep',
  'medication',
  'check_in',
] as const

export type RoutineKind = (typeof ROUTINE_KINDS)[number]

export interface RoutineSchedule {
  /** Wall-clock times, "HH:MM" (the routine's timezone). */
  times: string[]
  /** ISO weekdays 0=Mon … 6=Sun; empty/absent = every day. */
  weekdays: number[]
  /** IANA timezone (e.g. "America/Los_Angeles"). */
  timezone?: string
}

export interface Routine {
  id: string
  kind: RoutineKind
  title: string
  schedule: RoutineSchedule
  enabled: boolean
  last_completed_at: string | null
  deterministic_fallback_alarm: Record<string, unknown>
  missed_flags: Record<string, string>
  created_at: string | null
  updated_at: string | null
}

export interface RoutineCreatePayload {
  kind: RoutineKind
  title: string
  schedule?: RoutineSchedule
  enabled?: boolean
}

/** Field-level update — only title/kind/schedule/enabled are accepted. */
export interface RoutinePatchPayload {
  title?: string
  kind?: RoutineKind
  schedule?: RoutineSchedule | null
  enabled?: boolean
}

export interface CheckInPayload {
  /** Defaults to now server-side; idempotent per (routine, UTC day). */
  at?: string
  /** A skip (``false``) is still "handled" — no guilt either way. */
  completed?: boolean
  note?: string | null
}

export interface CheckInResult {
  check_in: {
    id: string
    routine_id: string
    at: string
    completed: boolean
    note: string | null
    created_at: string
  }
  executed: boolean
  reason?: string
}

export interface CompleteResult {
  routine: Routine
  executed: boolean
  reason?: string
}

/** One evaluated schedule slot (due now or upcoming). */
export interface RoutineSlot {
  id: string
  kind: 'routine'
  routine_id: string
  title: string
  /** Local wall-clock label, e.g. "07:00". */
  time: string
  at: string
  deadline_at: string
  timezone: string
  state: 'due' | 'upcoming'
}

/** The ONE gentle note for a missed window (never a streak). */
export interface MissedNote {
  id: string
  kind: 'routine_missed'
  routine_id: string
  title: string
  window_key: string
  slot: string
  note: string
  at: string
}

export interface SleepSummary {
  regularity: 'unknown' | 'stable' | 'variable'
  avg_minutes: number | null
  n: number
  description?: string
}

export interface RoutinesToday {
  due_now: RoutineSlot[]
  upcoming: RoutineSlot[]
  missed_notes: MissedNote[]
  completed_today: {
    id: string
    routine_id: string
    at: string
    completed: boolean
    note: string | null
    created_at: string
  }[]
  sleep: SleepSummary
  durable: boolean
}

export interface RoutinesListResponse {
  data: Routine[]
  durable: boolean
}

export interface RoutineCreatedResponse {
  routine: Routine
  created: boolean
}

export interface RoutinePatchedResponse {
  routine: Routine
}

/** Empty shapes used in mock mode (no backend configured). */
const EMPTY_LIST: RoutinesListResponse = { data: [], durable: false }
const EMPTY_TODAY: RoutinesToday = {
  due_now: [],
  upcoming: [],
  missed_notes: [],
  completed_today: [],
  sleep: { regularity: 'unknown', avg_minutes: null, n: 0 },
  durable: false,
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* keep the status detail */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

export const routinesApi = {
  /** List routines (optionally enabled-only). */
  async list(enabledOnly = false): Promise<RoutinesListResponse> {
    if (useMockMode) return EMPTY_LIST
    const suffix = enabledOnly ? '?enabled_only=true' : ''
    const res = await apiFetch(`/api/routines/${suffix}`)
    return json<RoutinesListResponse>(res)
  },

  /** Create a routine (201). */
  async create(payload: RoutineCreatePayload): Promise<RoutineCreatedResponse> {
    if (useMockMode) throw new Error('routines are not persisted in mock mode')
    const res = await apiFetch('/api/routines/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return json<RoutineCreatedResponse>(res)
  },

  /** Field-level update (title/kind/schedule/enabled). */
  async patch(id: string, payload: RoutinePatchPayload): Promise<RoutinePatchedResponse> {
    if (useMockMode) throw new Error('routines are not persisted in mock mode')
    const res = await apiFetch(`/api/routines/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return json<RoutinePatchedResponse>(res)
  },

  /** Idempotent per (routine, UTC day) check-in. */
  async checkin(id: string, payload: CheckInPayload = {}): Promise<CheckInResult> {
    if (useMockMode) throw new Error('routines are not persisted in mock mode')
    const res = await apiFetch(`/api/routines/${encodeURIComponent(id)}/checkin`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return json<CheckInResult>(res)
  },

  /** Complete = completed check-in + completion stamp. */
  async complete(id: string): Promise<CompleteResult> {
    if (useMockMode) throw new Error('routines are not persisted in mock mode')
    const res = await apiFetch(`/api/routines/${encodeURIComponent(id)}/complete`, {
      method: 'POST',
    })
    return json<CompleteResult>(res)
  },

  /** The routines contribution to Today (due/upcoming/notes/sleep). */
  async today(): Promise<RoutinesToday> {
    if (useMockMode) return { ...EMPTY_TODAY }
    const res = await apiFetch('/api/routines/today')
    return json<RoutinesToday>(res)
  },
}
