/**
 * Bills API client (Personal-Ops Recovery §7 — workstream G).
 *
 * Obligation records only. **Hard rule:** NerveCenter never transfers money,
 * never purchases, and never assumes payment — confirming a bill merely
 * records the user's explicit "I paid" statement. This client only talks to
 * the local NerveCenter API.
 */

import { apiFetch, useMockMode } from './client'

export type BillRecurrence = 'one_time' | 'weekly' | 'monthly' | 'yearly' | 'other'
export type BillStatus = 'pending' | 'confirmed_paid' | 'disputed' | 'stale'

export interface Bill {
  id: string
  payee: string
  amount_cents: string
  currency: string
  due_at: string
  recurrence: string
  interval: Record<string, unknown>
  status: BillStatus
  confirmed_at: string | null
  evidence: Record<string, unknown>[]
  duplicate_of: string | null
  created_at: string | null
  updated_at: string | null
  /** Deterministic escalation ladder: 0–3, or null when no escalation. */
  escalation_level?: number | null
}

export interface BillObligation {
  bill_id: string
  payee: string
  amount_cents: string
  currency: string
  status: BillStatus
}

export interface CalendarDay {
  date: string
  obligations: BillObligation[]
}

export interface Calendar {
  month: string
  days: CalendarDay[]
  num_days?: number
  durable?: boolean
}

export interface BillCreatePayload {
  payee: string
  /** Amount in minor units (e.g. 14250 = $142.50). */
  amount_cents: number
  currency?: string
  due_at: string
  recurrence?: BillRecurrence
  interval?: Record<string, unknown>
}

export type BillPatchPayload = Partial<BillCreatePayload> & {
  status?: 'pending' | 'disputed'
  recurrence?: BillRecurrence
  interval?: Record<string, unknown> | null
}

export interface BillsResponse {
  bills: Bill[]
  durable: boolean
}

export interface UpcomingResponse {
  bills: Bill[]
  days: number
  durable: boolean
}

export interface BillActionResponse {
  bill: Bill
  executed: boolean
  reason?: string
}

/** Empty shapes used in mock mode (no backend configured). */
const EMPTY_BILLS: BillsResponse = { bills: [], durable: false }
const EMPTY_UPCOMING: UpcomingResponse = { bills: [], days: 14, durable: false }
const EMPTY_CALENDAR = (month: string): Calendar => ({ month, days: [], durable: false })

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

export const billsApi = {
  /** List bills, optionally filtered by status. */
  async listBills(status?: BillStatus): Promise<BillsResponse> {
    if (useMockMode) return EMPTY_BILLS
    const suffix = status ? `?status=${status}` : ''
    const res = await apiFetch(`/api/bills/${suffix}`)
    return json<BillsResponse>(res)
  },

  /** Create a bill (always `pending` on create). */
  async createBill(payload: BillCreatePayload): Promise<BillActionResponse> {
    if (useMockMode) throw new Error('bills are not persisted in mock mode')
    const res = await apiFetch('/api/bills/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return json<BillActionResponse>(res)
  },

  /** Bills due within `days` (confirmed_paid excluded server-side). */
  async upcoming(days = 14): Promise<UpcomingResponse> {
    if (useMockMode) return { ...EMPTY_UPCOMING, days }
    const res = await apiFetch(`/api/bills/upcoming?days=${days}`)
    return json<UpcomingResponse>(res)
  },

  /** Obligations grouped by day for one month (`YYYY-MM`). */
  async calendar(month: string): Promise<Calendar> {
    if (useMockMode) return EMPTY_CALENDAR(month)
    const res = await apiFetch(`/api/bills/calendar?month=${month}`)
    return json<Calendar>(res)
  },

  /** The ONLY paid-state writer — records the user's explicit confirmation. */
  async confirmPaid(billId: string): Promise<BillActionResponse> {
    if (useMockMode) throw new Error('bills are not persisted in mock mode')
    const res = await apiFetch(`/api/bills/${billId}/confirm-paid`, { method: 'POST' })
    return json<BillActionResponse>(res)
  },

  /** Field-level edit; status only to `pending` or `disputed`. */
  async patchBill(billId: string, patch: BillPatchPayload): Promise<BillActionResponse> {
    if (useMockMode) throw new Error('bills are not persisted in mock mode')
    const res = await apiFetch(`/api/bills/${billId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    return json<BillActionResponse>(res)
  },
}
