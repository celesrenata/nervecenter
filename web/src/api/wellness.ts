/**
 * Wellness Journal API client (Personal-Ops Recovery §10 — workstream I).
 *
 * Private, user-controlled wellness check-ins. **Privacy:** wellness data
 * stays in NerveCenter and is never sent to other services or models —
 * this client only talks to the local NerveCenter API. No inference.
 */

import { apiFetch, useMockMode } from './client'

export interface WellnessEntry {
  id: string
  at: string
  mood: number | null
  sleep_minutes: number | null
  sleep_consistency: Record<string, unknown> | null
  energy: number | null
  stress: number | null
  journal_entry: string | null
  observations: Record<string, unknown> | null
  created_at: string | null
  updated_at: string | null
  sensitivity: string
}

export interface WellnessStatus {
  enabled: boolean
  paused_until: string | null
  entry_count: number
  last_entry_at: string | null
  sensitivity: string
  privacy_statement: string
  durable?: boolean
}

export interface TrendBlock {
  metric: string
  window_days: number
  n: number
  mean: number | null
  first: number | null
  last: number | null
  slope_per_day: number | null
  relative_change: number | null
  direction: 'upward' | 'downward' | 'flat' | 'insufficient'
  description: string
}

export interface SleepConsistency {
  n: number
  avg_minutes: number | null
  stdev_minutes: number | null
  regularity: 'stable' | 'variable' | 'unknown'
  description: string
}

export interface CorrelationBlock {
  metric: string
  n: number
  r: number | null
  status: 'ok' | 'insufficient' | 'no_variation'
  description: string
}

export interface WellnessTrends {
  sensitivity?: string
  window_days: number
  n_entries: number
  trends: Record<string, TrendBlock>
  sleep: SleepConsistency
  correlation: CorrelationBlock
  sleep_data_present: boolean
  durable?: boolean
}

export interface CheckInPayload {
  at?: string
  mood?: number | null
  sleep_minutes?: number | null
  sleep_consistency?: Record<string, unknown> | null
  energy?: number | null
  stress?: number | null
  journal_entry?: string | null
  observations?: Record<string, unknown> | null
}

export interface ExportResult {
  format: string
  entries: number
  audit: { code: string; id: string }
  window: string
  privacy_statement: string
}

const PRIVACY_STATEMENT =
  'Wellness data stays in NerveCenter and is never sent to other services or models.'

const EMPTY_STATUS: WellnessStatus = {
  enabled: true,
  paused_until: null,
  entry_count: 0,
  last_entry_at: null,
  sensitivity: 'personal',
  privacy_statement: PRIVACY_STATEMENT,
  durable: false,
}

const EMPTY_TRENDS: WellnessTrends = {
  window_days: 30,
  n_entries: 0,
  trends: {},
  sleep: {
    n: 0,
    avg_minutes: null,
    stdev_minutes: null,
    regularity: 'unknown',
    description: 'no sleep data',
  },
  correlation: {
    metric: 'sleep_vs_mood',
    n: 0,
    r: null,
    status: 'insufficient',
    description: 'insufficient data',
  },
  sleep_data_present: false,
  durable: false,
}

/** In-memory demo store used only when no real API base is configured. */
const demoEntries: WellnessEntry[] = []
let demoStatus = { enabled: true, paused_until: null as string | null }
let demoSeq = 0

async function json<T>(res: Response): Promise<T> {
  return (await res.json()) as T
}

export const wellnessApi = {
  async getStatus(): Promise<WellnessStatus> {
    if (useMockMode) {
      return {
        ...EMPTY_STATUS,
        entry_count: demoEntries.length,
        last_entry_at: demoEntries.length
          ? demoEntries[demoEntries.length - 1].at
          : null,
        enabled: demoStatus.enabled,
        paused_until: demoStatus.paused_until,
      }
    }
    const res = await apiFetch('/api/wellness/status')
    return json<WellnessStatus>(res)
  },

  async patchStatus(patch: {
    enabled?: boolean
    paused_until?: string | null
    clear_pause?: boolean
  }): Promise<WellnessStatus> {
    if (useMockMode) {
      if (typeof patch.enabled === 'boolean') demoStatus = { ...demoStatus, enabled: patch.enabled }
      if (patch.paused_until !== undefined)
        demoStatus = { ...demoStatus, paused_until: patch.paused_until }
      if (patch.clear_pause || patch.enabled === true)
        demoStatus = { ...demoStatus, paused_until: null }
      return wellnessApi.getStatus()
    }
    const res = await apiFetch('/api/wellness/status', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    return json<WellnessStatus>(res)
  },

  async listEntries(from?: string, to?: string): Promise<WellnessEntry[]> {
    if (useMockMode) {
      const qs = new URLSearchParams()
      if (from) qs.set('from', from)
      if (to) qs.set('to', to)
      return demoEntries
        .filter((e) => (!from || e.at >= from) && (!to || e.at <= to))
        .sort((a, b) => (a.at < b.at ? 1 : -1))
    }
    const qs = new URLSearchParams()
    if (from) qs.set('from', from)
    if (to) qs.set('to', to)
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    const res = await apiFetch(`/api/wellness/entries${suffix}`)
    return (await json<{ data: WellnessEntry[] }>(res)).data
  },

  async checkin(payload: CheckInPayload): Promise<{ entry: WellnessEntry; created: boolean }> {
    if (useMockMode) {
      const at = payload.at ?? new Date().toISOString()
      const day = at.slice(0, 10)
      const existing = demoEntries.find((e) => e.at.slice(0, 10) === day)
      const entry: WellnessEntry = {
        id: existing ? existing.id : `demo-${++demoSeq}`,
        at,
        mood: payload.mood ?? existing?.mood ?? null,
        sleep_minutes: payload.sleep_minutes ?? existing?.sleep_minutes ?? null,
        sleep_consistency: payload.sleep_consistency ?? null,
        energy: payload.energy ?? existing?.energy ?? null,
        stress: payload.stress ?? existing?.stress ?? null,
        journal_entry: payload.journal_entry ?? existing?.journal_entry ?? null,
        observations: payload.observations ?? existing?.observations ?? null,
        created_at: existing?.created_at ?? at,
        updated_at: at,
        sensitivity: 'personal',
      }
      if (existing) {
        const i = demoEntries.indexOf(existing)
        demoEntries[i] = entry
      } else {
        demoEntries.push(entry)
      }
      return { entry, created: !existing }
    }
    const res = await apiFetch('/api/wellness/entries', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return json<{ entry: WellnessEntry; created: boolean }>(res)
  },

  async updateEntry(
    id: string,
    patch: Omit<CheckInPayload, 'at'>
  ): Promise<WellnessEntry | null> {
    if (useMockMode) {
      const existing = demoEntries.find((e) => e.id === id)
      if (!existing) return null
      const entry: WellnessEntry = {
        ...existing,
        mood: patch.mood ?? existing.mood,
        sleep_minutes: patch.sleep_minutes ?? existing.sleep_minutes,
        sleep_consistency:
          patch.sleep_consistency !== undefined
            ? patch.sleep_consistency
            : existing.sleep_consistency,
        energy: patch.energy ?? existing.energy,
        stress: patch.stress ?? existing.stress,
        journal_entry:
          patch.journal_entry !== undefined ? patch.journal_entry : existing.journal_entry,
        observations:
          patch.observations !== undefined ? patch.observations : existing.observations,
        updated_at: new Date().toISOString(),
      }
      demoEntries[demoEntries.indexOf(existing)] = entry
      return entry
    }
    const res = await apiFetch(`/api/wellness/entries/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    if (!res.ok) return null
    return json<WellnessEntry>(res)
  },

  async deleteEntry(id: string): Promise<boolean> {
    if (useMockMode) {
      const i = demoEntries.findIndex((e) => e.id === id)
      if (i < 0) return false
      demoEntries.splice(i, 1)
      return true
    }
    const res = await apiFetch(`/api/wellness/entries/${id}`, { method: 'DELETE' })
    return res.ok
  },

  /** Wipe ALL wellness entries (hard delete). */
  async deleteAll(): Promise<number> {
    if (useMockMode) {
      const n = demoEntries.length
      demoEntries.length = 0
      return n
    }
    const res = await apiFetch('/api/wellness/entries', { method: 'DELETE' })
    return (await json<{ deleted: number }>(res)).deleted
  },

  async getTrends(days = 30): Promise<WellnessTrends> {
    if (useMockMode) return { ...EMPTY_TRENDS, window_days: days }
    const res = await apiFetch(`/api/wellness/trends?days=${days}`)
    return json<WellnessTrends>(res)
  },

  /**
   * Explicit clinician export. The backend records the export in an audit
   * log; returns the audit receipt on success, throws on 403 (disabled).
   */
  async export(format: 'csv' | 'json', from?: string, to?: string): Promise<ExportResult> {
    if (useMockMode) {
      return {
        format,
        entries: demoEntries.length,
        audit: { code: 'wellness_export', id: `demo-audit-${Date.now()}` },
        window: 'demo',
        privacy_statement: PRIVACY_STATEMENT,
      }
    }
    const qs = new URLSearchParams({ format })
    if (from) qs.set('from', from)
    if (to) qs.set('to', to)
    const res = await apiFetch(`/api/wellness/export?${qs.toString()}`)
    if (!res.ok) {
      const body = await res.text()
      throw new Error(res.status === 403 ? body || `HTTP ${res.status}` : `HTTP ${res.status}`)
    }
    if (format === 'csv') {
      const text = await res.text()
      const rows = text.split('\n').length - 1
      return {
        format,
        entries: Math.max(0, rows),
        audit: { code: 'wellness_export', id: res.headers.get('x-audit-id') ?? '' },
        window: '',
        privacy_statement: PRIVACY_STATEMENT,
      }
    }
    return json<ExportResult>(res)
  },

  async downloadCsv(from?: string, to?: string): Promise<void> {
    if (useMockMode) {
      const rows = [
        'id,at,mood,sleep_minutes,energy,stress,journal_entry,observations',
        ...demoEntries.map((e) =>
          [e.id, e.at, e.mood, e.sleep_minutes, e.energy, e.stress, e.journal_entry, e.observations ? JSON.stringify(e.observations) : '']
            .map((v) => `"${String(v ?? '').replace(/"/g, '""')}"`)
            .join(',')
        ),
      ].join('\n')
      const blob = new Blob([rows], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'wellness-export.csv'
      a.click()
      URL.revokeObjectURL(url)
      return
    }
    const qs = new URLSearchParams({ format: 'csv' })
    if (from) qs.set('from', from)
    if (to) qs.set('to', to)
    const res = await apiFetch(`/api/wellness/export?${qs.toString()}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'wellness-export.csv'
    a.click()
    URL.revokeObjectURL(url)
  },
}
