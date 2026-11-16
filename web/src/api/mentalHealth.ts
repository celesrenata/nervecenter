/** Mental-health tracker API client (NS-ADR-101). All routes sit behind SSO. */
import { apiFetch, apiErrorFromResponse } from './client'

export interface MhInstrument {
  key: 'phq9' | 'gad7'
  title: string
  prompt: string
  items: string[]
  choices: { value: number; label: string }[]
  max_score: number
}

export interface MhPoint {
  date: string
  value: number
}

export interface MhInstrumentTrend {
  title: string
  max: number
  count: number
  latest: { date: string; total: number; band: string } | null
  daily: MhPoint[]
  rolling_7: MhPoint[]
  rolling_30: MhPoint[]
  change_point: { date: string; before_mean: number; after_mean: number; shift: number } | null
  streaks: { threshold: number; longest: number; current: number }
}

export interface MhTrends {
  days: number
  instruments: Record<string, MhInstrumentTrend>
}

export interface MhCorrelationRow {
  instrument: string
  factor: string
  label: string
  state: 'ok' | 'too_few_samples'
  n: number
  r: number | null
  lag: number | null
  min_samples: number
}

export interface MhCorrelations {
  days: number
  min_samples: number
  note: string
  rows: MhCorrelationRow[]
}

export interface MhHistoryEntry {
  date: string
  instrument: string
  total: number
  max: number
  band: string
  answers: number[]
  comment: string | null
}

async function getJson<T>(path: string): Promise<T> {
  const res = await apiFetch(path)
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

export const mentalHealthApi = {
  instruments: async () => (await getJson<{ instruments: MhInstrument[] }>('/api/mental-health/instruments')).instruments,
  trends: (days = 90) => getJson<MhTrends>(`/api/mental-health/trends?days=${days}`),
  correlations: (days = 90) => getJson<MhCorrelations>(`/api/mental-health/correlations?days=${days}`),
  history: async (days = 30) =>
    (await getJson<{ entries: MhHistoryEntry[] }>(`/api/mental-health/history?days=${days}`)).entries,
  async submit(body: { instrument: string; answers: number[]; comment?: string }) {
    const res = await apiFetch('/api/mental-health/forms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as { total: number; band: string; max: number; created: boolean }
  },
}
