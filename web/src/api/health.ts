/**
 * Apple Health API client (NS-ADR-100). Read-only: the iPhone app pushes the
 * data; this view only reads `GET /api/health/summary` (behind SSO).
 */
import { apiFetch, apiErrorFromResponse } from './client'

export interface HealthDailyPoint {
  date: string
  value: number
}

export interface HealthKindSummary {
  unit: string
  count: number
  latest: { at: string; value: number } | null
  mean: number | null
  min: number | null
  max: number | null
  daily: HealthDailyPoint[]
}

export interface HealthSummary {
  configured: boolean
  days: number
  last_sync_at: string | null
  total_samples: number
  kinds: Record<string, HealthKindSummary>
}

export const healthApi = {
  async getSummary(days = 30): Promise<HealthSummary> {
    const res = await apiFetch(`/api/health/summary?days=${days}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as HealthSummary
  },
}
