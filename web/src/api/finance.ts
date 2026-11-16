/** Financial helper API client (NS-ADR-102). Read-only: no route pays anything. */
import { apiFetch, apiErrorFromResponse } from './client'

export const INSTITUTIONS = ['BECU', 'KeyBank', 'Chase', 'Apple Card', 'PayPal', 'other'] as const
export type Institution = (typeof INSTITUTIONS)[number]

export interface DebtAccount {
  id: string
  institution: Institution
  kind: string
  name: string
  balance_cents: number
  apr_pct: string
  min_payment_cents: number
  due_day: number | null
  as_of: string
}
export interface NewAccount {
  institution: Institution
  kind: 'credit_card' | 'loan' | 'line_of_credit' | 'other'
  name: string
  balance_cents: number
  apr_pct: string
  min_payment_cents: number
  due_day?: number
}
export interface PlanMonth {
  month: number
  date: string
  interest_cents: number
  balance_cents: number
  payments: Record<string, number>
}
export interface PlanResult {
  never_reached: boolean
  months: number | null
  payoff_date: string | null
  total_interest_cents: number | null
  remaining_cents: number
  interest_saved_cents?: number | null
  schedule?: PlanMonth[]
}
export interface Plan {
  extra_cents: number
  total_balance_cents: number
  minimums: PlanResult
  avalanche: PlanResult
  snowball: PlanResult
}
export interface Reminder {
  kind: 'bill' | 'card_due'
  id: string
  title: string
  due_date: string
  days_until: number
  level: number
  overdue?: boolean
  amount_cents: number
  amount_label: string
}
export interface ImportResult {
  duplicate: boolean
  added: number
  rows: number
  format?: string
}
export interface ImportRecord {
  id: string
  institution: string
  format: string
  row_count: number
  added_count: number
  created_at: string | null
}

async function getJson<T>(path: string): Promise<T> {
  const res = await apiFetch(path)
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

export const financeApi = {
  accounts: async () => (await getJson<{ accounts: DebtAccount[] }>('/api/finance/accounts')).accounts,
  plan: (extraCents: number) => getJson<Plan>(`/api/finance/plan?extra_cents=${extraCents}`),
  reminders: async () => (await getJson<{ reminders: Reminder[] }>('/api/finance/reminders')).reminders,
  imports: async () => (await getJson<{ imports: ImportRecord[] }>('/api/finance/imports')).imports,
  async createAccount(body: NewAccount) {
    const res = await apiFetch('/api/finance/accounts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DebtAccount
  },
  async deleteAccount(id: string) {
    const res = await apiFetch(`/api/finance/accounts/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (!res.ok) throw await apiErrorFromResponse(res)
  },
  async importStatement(file: File, institution: Institution) {
    const res = await apiFetch(`/api/finance/imports?institution=${encodeURIComponent(institution)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as ImportResult
  },
}
