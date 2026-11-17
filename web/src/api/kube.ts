/**
 * Kube agent API (NS-ADR-120). Modes and overrides are decided by the backend; the page only
 * shows them and sends the owner's clicks.
 */
import { apiFetch, apiErrorFromResponse } from './client'

export type KubeMode = 'normal' | 'known_down' | 'diagnose_only'

export interface KubeOverride {
  id: string
  issue_key: string
  namespace: string
  created_by: string
  created_at: string | null
  expires_at: string | null
  revoked_at: string | null
}

export interface KubeIssue {
  key: string
  namespace: string
  kind: string
  name: string
  reason: string
  severity: string
  mode: KubeMode
  first_seen: string | null
  last_seen: string | null
  evidence: Record<string, unknown>
  override: KubeOverride | null
}

export interface KubeSettings {
  protected_namespaces: string[]
  known_down: string[]
  override_hours: number
  retention_daily: number
  retention_weekly: number
  daily_run_cap: number
  issue_cooldown_hours: number
  prometheus_url: string | null
  diagnose_only_until_first_report: boolean
  last_check_at: string | null
  last_waste_at: string | null
  updated_by: string | null
}

export interface KubeStatus {
  settings: KubeSettings
  last_check_at: string | null
  last_waste_at: string | null
  monitor: { running: boolean; last: Record<string, unknown> | null }
  counts: { open: number; known_down: number; diagnose_only: number; overrides: number }
}

export interface WasteItem {
  [field: string]: unknown
}
export interface WasteSection {
  count: number
  items: WasteItem[]
}
export interface WasteReport {
  collected_at: string
  metrics_available: boolean
  idle_pods: WasteSection
  oversized_pods: WasteSection
  unused_pvcs: WasteSection
  orphaned_pvs: WasteSection
  restarting_pods: WasteSection
  crash_loop_burn: WasteSection & { estimate: boolean; formula: string }
  not_measured: string[]
  warnings: string[]
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

const send = (path: string, method: string, body?: unknown) =>
  apiFetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })

export const kubeApi = {
  status: async () => json<KubeStatus>(await apiFetch('/api/kube/status')),
  issues: async () =>
    json<{ issues: KubeIssue[]; overrides: KubeOverride[] }>(await apiFetch('/api/kube/issues')),
  waste: async () =>
    (await json<{ report: { created_at: string; data: WasteReport } | null }>(await apiFetch('/api/kube/waste')))
      .report,
  saveSettings: async (patch: Partial<KubeSettings>) =>
    json<KubeSettings>(await send('/api/kube/settings', 'PUT', patch)),
  checkNow: async () => json<Record<string, unknown>>(await send('/api/kube/check', 'POST')),
  override: async (key: string) =>
    json<KubeOverride>(await send(`/api/kube/issues/${encodeURIComponent(key)}/override`, 'POST')),
  revoke: async (id: string) => {
    const res = await send(`/api/kube/overrides/${encodeURIComponent(id)}`, 'DELETE')
    if (!res.ok) throw await apiErrorFromResponse(res)
  },
  enableProposals: async () => json<KubeSettings>(await send('/api/kube/enable-proposals', 'POST')),
}
