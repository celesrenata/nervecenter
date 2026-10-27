import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  billsApi,
  type Bill,
  type BillRecurrence,
  type BillStatus,
  type Calendar,
} from '../api/bills'

/**
 * BillsView — personal bills / obligations (Personal-Ops Recovery §7).
 *
 * Obligation records only: what the user may have to pay, when, and whether
 * the user has *said* they paid. **NerveCenter never pays and never assumes
 * payment** — "Confirm paid" is a two-step user action that merely records
 * the confirmation; there is no payment path anywhere in the bills plane.
 *
 * Sections: bill list (status + escalation badges), upcoming (14 days),
 * monthly obligation grid (obligations per day, empty cells otherwise),
 * and an add-bill form. Degrades to an honest "demo mode — not persisted"
 * state when no durable registry is configured (GETs return empty shapes).
 */

// ─── helpers ────────────────────────────────────────────────────────────────

function fmtAmount(amountCents: string, currency: string): string {
  const cents = Number(amountCents)
  if (!Number.isFinite(cents)) return amountCents
  const major = cents / 100
  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: currency || 'USD',
  }).format(major)
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })
}

function monthLabel(month: string): string {
  const [y, m] = month.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString([], {
    month: 'long',
    year: 'numeric',
  })
}

function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split('-').map(Number)
  const d = new Date(Date.UTC(y, m - 1 + delta, 1))
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`
}

function currentMonth(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function statusBadge(status: BillStatus): { label: string; cls: string } {
  switch (status) {
    case 'pending':
      return { label: 'pending', cls: 'bg-hover text-secondary' }
    case 'confirmed_paid':
      return { label: 'confirmed paid', cls: 'bg-success/20 text-success' }
    case 'disputed':
      return { label: 'disputed', cls: 'bg-warning/20 text-warning' }
    case 'stale':
      return { label: 'stale — due, unconfirmed', cls: 'bg-danger/20 text-danger' }
  }
}

function escalationBadge(level: number | null | undefined): { label: string; cls: string } | null {
  switch (level) {
    case 1:
      return { label: 'due within 7 days', cls: 'bg-warning/20 text-warning' }
    case 2:
      return { label: 'due within 48h', cls: 'bg-warning/20 text-warning' }
    case 3:
      return { label: 'overdue', cls: 'bg-danger/20 text-danger' }
    default:
      return null
  }
}

// ─── confirm-paid button (two-step) ─────────────────────────────────────────

/**
 * Two-step confirmation: the first click arms the button (and shows the
 * honest "never pays" copy), the second click executes. Auto-disarms after
 * a short window so a stale armed state cannot surprise the user.
 */
function ConfirmPaidButton({
  bill,
  armed,
  busy,
  onArm,
  onConfirm,
}: {
  bill: Bill
  armed: boolean
  busy: boolean
  onArm: (id: string) => void
  onConfirm: (id: string) => void
}) {
  if (bill.status === 'confirmed_paid') {
    return (
      <span className="text-xs text-muted" title="NerveCenter never pays — this records your confirmation.">
        confirmed {fmtDate(bill.confirmed_at)}
      </span>
    )
  }
  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={() => (armed ? onConfirm(bill.id) : onArm(bill.id))}
        disabled={busy}
        className={`rounded px-3 py-1.5 text-xs font-medium disabled:opacity-40 ${
          armed
            ? 'bg-accent text-accent-contrast'
            : 'border border-line text-secondary hover:bg-hover'
        }`}
      >
        {armed ? 'Confirm — record my payment' : 'Confirm paid'}
      </button>
      {armed && (
        <span className="text-[11px] leading-tight text-secondary">
          NerveCenter never pays this. This only records that you confirmed
          the payment.
        </span>
      )}
    </div>
  )
}

// ─── add-bill form ──────────────────────────────────────────────────────────

const RECURRENCES: { value: BillRecurrence; label: string }[] = [
  { value: 'one_time', label: 'One-time' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'yearly', label: 'Yearly' },
  { value: 'other', label: 'Other' },
]

function AddBillForm({ busy, onAdded }: { busy: boolean; onAdded: () => void }) {
  const [submitting, setSubmitting] = useState(false)
  const [payee, setPayee] = useState('')
  const [amount, setAmount] = useState('')
  const [currency, setCurrency] = useState('USD')
  const [due, setDue] = useState('')
  const [recurrence, setRecurrence] = useState<BillRecurrence>('one_time')
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (submitting || busy) return
    setError(null)
    const cents = Math.round(parseFloat(amount) * 100)
    if (!payee.trim()) return setError('Who is this bill from?')
    if (!Number.isFinite(cents) || cents <= 0) return setError('Enter an amount greater than zero.')
    if (!due) return setError('Pick a due date.')
    setSubmitting(true)
    try {
      await billsApi.createBill({
        payee: payee.trim(),
        amount_cents: cents,
        currency: currency.trim().toUpperCase() || 'USD',
        due_at: due,
        recurrence,
      })
      setPayee('')
      setAmount('')
      setDue('')
      onAdded()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add the bill')
    } finally {
      setSubmitting(false)
    }
  }

  const inputCls =
    'w-full rounded border border-line bg-transparent px-2 py-1.5 text-sm text-primary placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-accent'

  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Add a bill">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-xs text-secondary">
          <span className="mb-1 block">Payee</span>
          <input
            type="text"
            value={payee}
            onChange={(e) => setPayee(e.target.value)}
            placeholder="e.g. Acme Power"
            className={inputCls}
          />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block text-xs text-secondary">
            <span className="mb-1 block">Amount</span>
            <input
              type="number"
              min="0.01"
              step="0.01"
              inputMode="decimal"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder="142.50"
              className={inputCls}
            />
          </label>
          <label className="block text-xs text-secondary">
            <span className="mb-1 block">Currency</span>
            <input
              type="text"
              value={currency}
              onChange={(e) => setCurrency(e.target.value)}
              maxLength={3}
              className={inputCls}
            />
          </label>
        </div>
        <label className="block text-xs text-secondary">
          <span className="mb-1 block">Due</span>
          <input
            type="date"
            value={due}
            onChange={(e) => setDue(e.target.value)}
            className={inputCls}
          />
        </label>
        <label className="block text-xs text-secondary">
          <span className="mb-1 block">Repeats</span>
          <select
            value={recurrence}
            onChange={(e) => setRecurrence(e.target.value as BillRecurrence)}
            className={inputCls}
          >
            {RECURRENCES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={busy || submitting}
        className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-accent-contrast disabled:opacity-40"
      >
        {submitting ? 'Adding…' : 'Add bill'}
      </button>
      <p className="text-[11px] text-muted">
        Records an obligation only — NerveCenter never pays bills and never
        assumes payment.
      </p>
    </form>
  )
}

// ─── monthly grid ───────────────────────────────────────────────────────────

function MonthlyCalendar({ calendar }: { calendar: Calendar }) {
  const [month, monthNum] = calendar.month.split('-').map(Number)
  const daysInMonth =
    calendar.num_days ?? new Date(Date.UTC(month, monthNum, 0)).getUTCDate()
  const firstDow = (new Date(Date.UTC(month, monthNum - 1, 1)).getUTCDay() + 6) % 7 // Monday-first
  const byDay = useMemo(() => {
    const map = new Map<string, Calendar['days'][number]['obligations']>()
    for (const d of calendar.days) map.set(d.date, d.obligations)
    return map
  }, [calendar])

  const cells: (number | null)[] = [
    ...Array.from({ length: firstDow }, () => null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ]
  const now = new Date()
  const isCurrentMonth =
    `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}` === calendar.month
  const today = isCurrentMonth ? now.getDate() : null

  return (
    <div className="rounded-lg border border-line bg-elevated p-4">
      <h3 className="text-sm font-semibold text-primary">{monthLabel(calendar.month)}</h3>
      <div className="mt-3 grid grid-cols-7 gap-1 text-center">
        {WEEKDAYS.map((w) => (
          <div key={w} className="pb-1 text-[10px] font-medium text-muted">
            {w}
          </div>
        ))}
        {cells.map((day, i) => {
          if (day === null) return <div key={`b${i}`} className="min-h-[56px]" />
          const dayKey = `${calendar.month}-${String(day).padStart(2, '0')}`
          const isToday = day === today
          const obligations = byDay.get(dayKey) ?? []
          return (
            <div
              key={dayKey}
              className={`min-h-[56px] rounded border p-1 text-left ${
                obligations.length ? 'border-accent/40 bg-hover' : 'border-line'
              }`}
            >
              <div
                className={`text-[10px] tabular-nums ${
                  isToday ? 'font-semibold text-accent' : 'text-muted'
                }`}
              >
                {day}
              </div>
              <div className="mt-0.5 space-y-0.5">
                {obligations.slice(0, 2).map((o) => (
                  <div key={o.bill_id} className="text-[10px] leading-tight">
                    <span
                      className={`mr-0.5 inline-block h-1.5 w-1.5 rounded-full align-middle ${
                        o.status === 'confirmed_paid'
                          ? 'bg-success'
                          : o.status === 'stale'
                            ? 'bg-danger'
                            : o.status === 'disputed'
                              ? 'bg-warning'
                              : 'bg-accent'
                      }`}
                      title={o.status}
                    />
                    <span className="text-secondary" title={`${o.payee} — ${o.status}`}>
                      {o.payee}
                    </span>
                  </div>
                ))}
                {obligations.length > 2 && (
                  <div className="text-[10px] text-muted">+{obligations.length - 2} more</div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ─── main view ─────────────────────────────────────────────────────────────

export default function BillsView() {
  const [bills, setBills] = useState<Bill[]>([])
  const [upcoming, setUpcoming] = useState<Bill[]>([])
  const [calendar, setCalendar] = useState<Calendar | null>(null)
  const [month, setMonth] = useState<string>(() => currentMonth())
  const [durable, setDurable] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [armedId, setArmedId] = useState<string | null>(null)

  // Auto-disarm the two-step confirmation so a stale armed button cannot
  // record a confirmation the user no longer means.
  useEffect(() => {
    if (armedId === null) return
    const t = window.setTimeout(() => setArmedId(null), 8000)
    return () => window.clearTimeout(t)
  }, [armedId])

  const flash = (msg: string) => {
    setNotice(msg)
    window.setTimeout(() => setNotice(null), 4000)
  }

  const refresh = useCallback(async (m: string) => {
    try {
      const [list, up, cal] = await Promise.all([
        billsApi.listBills(),
        billsApi.upcoming(14),
        billsApi.calendar(m),
      ])
      setBills(list.bills)
      setUpcoming(up.bills)
      setCalendar(cal)
      setDurable(list.durable)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load bills')
    }
  }, [])

  useEffect(() => {
    void refresh(month)
  }, [refresh, month])

  const confirmPaid = async (billId: string) => {
    setBusy(true)
    setArmedId(null)
    try {
      const res = await billsApi.confirmPaid(billId)
      flash(
        res.executed
          ? `Recorded: you confirmed payment for ${res.bill.payee}. Nothing was paid by NerveCenter.`
          : 'Not recorded — bills storage is not configured.'
      )
      await refresh(month)
    } catch (e) {
      flash(e instanceof Error ? e.message : 'Could not record the confirmation')
    } finally {
      setBusy(false)
    }
  }

  const openAddForm = () => {
    // gentle scroll cue for the add form on narrow screens
    document.getElementById('add-bill')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }

  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const b of bills) counts[b.status] = (counts[b.status] ?? 0) + 1
    return counts
  }, [bills])

  return (
    <div className="space-y-6">
      {/* header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-primary">Bills</h1>
          <p className="mt-1 max-w-2xl text-sm text-secondary">
            Obligation records only. NerveCenter never pays, never purchases,
            and never assumes payment — confirming records that <em>you</em>
            say you paid.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted">
          {durable === false && (
            <span className="rounded bg-hover px-2 py-1">demo mode — not persisted</span>
          )}
          <span className="rounded bg-hover px-2 py-1 tabular-nums">
            {bills.length} bills
            {statusCounts.pending ? ` · ${statusCounts.pending} pending` : ''}
            {statusCounts.stale ? ` · ${statusCounts.stale} stale` : ''}
          </span>
        </div>
      </div>

      {error && (
        <div role="alert" className="rounded border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
          {error}
        </div>
      )}
      {notice && (
        <div role="status" className="rounded border border-line bg-elevated p-3 text-sm text-secondary">
          {notice}
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        {/* ── bill list ─────────────────────────────────────────────────── */}
        <section aria-label="Bills" className="lg:col-span-2">
          <div className="flex items-baseline justify-between">
            <h2 className="text-lg font-semibold text-primary">All bills</h2>
            <button
              type="button"
              onClick={openAddForm}
              className="text-xs text-accent hover:underline"
            >
              + add
            </button>
          </div>
          {bills.length === 0 ? (
            <div className="mt-3 rounded-lg border border-line bg-elevated p-6 text-center text-sm text-muted">
              No bills yet. Add one below, or wait for one to be proposed from
              your mail — nothing is assumed until you record it.
            </div>
          ) : (
            <div className="mt-3 overflow-x-auto rounded-lg border border-line bg-elevated">
              <table className="w-full min-w-[560px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="px-3 py-2 font-medium">Payee</th>
                    <th className="px-3 py-2 text-right font-medium">Amount</th>
                    <th className="px-3 py-2 font-medium">Due</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {bills.map((b) => {
                    const status = statusBadge(b.status)
                    const esc = escalationBadge(b.escalation_level)
                    return (
                      <tr key={b.id} className="border-b border-line/60 last:border-b-0">
                        <td className="px-3 py-2 text-primary">{b.payee}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-secondary">
                          {fmtAmount(b.amount_cents, b.currency)}
                        </td>
                        <td className="px-3 py-2 text-secondary">{fmtDate(b.due_at)}</td>
                        <td className="px-3 py-2">
                          <div className="flex flex-wrap items-center gap-1">
                            <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${status.cls}`}>
                              {status.label}
                            </span>
                            {esc && (
                              <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${esc.cls}`}>
                                {esc.label}
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right">
                          <ConfirmPaidButton
                            bill={b}
                            armed={armedId === b.id}
                            busy={busy}
                            onArm={setArmedId}
                            onConfirm={confirmPaid}
                          />
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {/* ── right rail: upcoming + add ────────────────────────────────── */}
        <div className="space-y-6">
          <section
            aria-label="Upcoming"
            className="rounded-lg border border-line bg-elevated p-4"
          >
            <h3 className="text-sm font-semibold text-primary">
              Upcoming <span className="text-xs font-normal text-muted">(next 14 days)</span>
            </h3>
            {upcoming.length === 0 ? (
              <p className="mt-2 text-xs text-muted">
                Nothing due in the next two weeks.
              </p>
            ) : (
              <ul className="mt-2 space-y-2">
                {upcoming.map((b) => (
                  <li
                    key={b.id}
                    className="flex items-center justify-between gap-2 rounded border border-line p-2"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-xs font-medium text-primary">{b.payee}</p>
                      <p className="text-[11px] text-muted">
                        {fmtDate(b.due_at)} · {fmtAmount(b.amount_cents, b.currency)}
                      </p>
                    </div>
                    {b.escalation_level !== null && b.escalation_level !== undefined && (
                      <span
                        className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${escalationBadge(b.escalation_level)?.cls ?? 'bg-hover text-muted'}`}
                      >
                        {escalationBadge(b.escalation_level)?.label}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section
            aria-label="Add a bill"
            id="add-bill"
            className="rounded-lg border border-line bg-elevated p-4"
          >
            <h3 className="text-sm font-semibold text-primary">Add a bill</h3>
            <div className="mt-3">
              <AddBillForm busy={busy} onAdded={() => { flash('Bill added.'); void refresh(month) }} />
            </div>
          </section>
        </div>
      </div>

      {/* ── monthly grid ──────────────────────────────────────────────────── */}
      <section aria-label="Monthly obligations">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold text-primary">Monthly obligations</h2>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => setMonth((m) => shiftMonth(m, -1))}
              className="rounded border border-line px-2.5 py-1 text-xs text-secondary hover:bg-hover"
              aria-label="Previous month"
            >
              ←
            </button>
            <span className="px-2 text-xs font-medium text-primary">
              {calendar ? monthLabel(calendar.month) : month}
            </span>
            <button
              type="button"
              onClick={() => setMonth((m) => shiftMonth(m, 1))}
              className="rounded border border-line px-2.5 py-1 text-xs text-secondary hover:bg-hover"
              aria-label="Next month"
            >
              →
            </button>
          </div>
        </div>
        {calendar ? (
          <div className="mt-3">
            <MonthlyCalendar calendar={calendar} />
          </div>
        ) : (
          <div className="mt-3 rounded-lg border border-line bg-elevated p-6 text-center text-sm text-muted">
            Loading calendar…
          </div>
        )}
      </section>
    </div>
  )
}
