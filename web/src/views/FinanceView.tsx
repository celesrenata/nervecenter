import { useCallback, useEffect, useState, type DragEvent } from 'react'
import {
  financeApi,
  INSTITUTIONS,
  type DebtAccount,
  type Institution,
  type Plan,
  type PlanResult,
  type Reminder,
} from '../api/finance'

/**
 * FinanceView (NS-ADR-102): debts you enter by hand, statement files you drop
 * in, an avalanche vs snowball comparison, and reminders. It is read-only: it
 * never pays, transfers or contacts a bank.
 */
const money = (cents: number) =>
  (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' })

function describe(r: PlanResult): string {
  if (r.never_reached) return 'Never paid off at these payments'
  return `${r.months} months, paid off ${r.payoff_date?.slice(0, 7)}, interest ${money(r.total_interest_cents ?? 0)}`
}

function PlanChart({ plan }: { plan: Plan }) {
  const series = [
    { key: 'minimums', label: 'Minimums only', r: plan.minimums, color: 'var(--muted, #888)', dash: '4 3' },
    { key: 'avalanche', label: 'Avalanche', r: plan.avalanche, color: 'var(--accent)', dash: undefined },
    { key: 'snowball', label: 'Snowball', r: plan.snowball, color: 'var(--primary, #ccc)', dash: '2 2' },
  ] as const
  const w = 360
  const h = 140
  const total = plan.total_balance_cents || 1
  const horizon = Math.max(2, ...series.map((s) => (s.r.months ?? 60) + 1))
  const x = (m: number) => (m / (horizon - 1)) * (w - 8) + 4
  const y = (v: number) => h - 6 - (v / total) * (h - 12)
  const points = (s: (typeof series)[number]) => {
    const sched = s.r.schedule
    if (!sched) {
      // Minimums have no schedule; draw a straight line to payoff (or flat when never reached).
      const end = s.r.months ?? horizon - 1
      return `${x(0)},${y(total)} ${x(end)},${y(s.r.never_reached ? total : 0)}`
    }
    return [`${x(0)},${y(total)}`, ...sched.map((m) => `${x(m.month)},${y(m.balance_cents)}`)].join(' ')
  }
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label="Remaining debt by month">
      {series.map((s) => (
        <polyline key={s.key} points={points(s)} fill="none" stroke={s.color} strokeWidth={2} strokeDasharray={s.dash} />
      ))}
    </svg>
  )
}

function AddAccount({ onAdded }: { onAdded: () => void }) {
  const [institution, setInstitution] = useState<Institution>('Chase')
  const [name, setName] = useState('')
  const [balance, setBalance] = useState('')
  const [apr, setApr] = useState('')
  const [minPay, setMinPay] = useState('')
  const [dueDay, setDueDay] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const valid = name.trim() !== '' && balance !== '' && apr !== '' && minPay !== ''
  const save = async () => {
    try {
      await financeApi.createAccount({
        institution,
        kind: 'credit_card',
        name: name.trim(),
        balance_cents: Math.round(Number(balance) * 100),
        apr_pct: apr,
        min_payment_cents: Math.round(Number(minPay) * 100),
        due_day: dueDay ? Number(dueDay) : undefined,
      })
      setName('')
      setBalance('')
      setApr('')
      setMinPay('')
      setDueDay('')
      setErr(null)
      onAdded()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not save')
    }
  }
  const input = 'mt-1 w-full rounded border border-border bg-transparent p-1 text-sm'
  return (
    <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-secondary">
      <label>Institution
        <select className={input} value={institution} onChange={(e) => setInstitution(e.target.value as Institution)}>
          {INSTITUTIONS.map((i) => <option key={i}>{i}</option>)}
        </select>
      </label>
      <label>Name<input className={input} value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>Balance ($)<input className={input} type="number" min="0" step="0.01" value={balance} onChange={(e) => setBalance(e.target.value)} /></label>
      <label>APR (%)<input className={input} type="number" min="0" max="100" step="0.01" value={apr} onChange={(e) => setApr(e.target.value)} /></label>
      <label>Minimum payment ($)<input className={input} type="number" min="0" step="0.01" value={minPay} onChange={(e) => setMinPay(e.target.value)} /></label>
      <label>Due day of month<input className={input} type="number" min="1" max="31" value={dueDay} onChange={(e) => setDueDay(e.target.value)} /></label>
      <button type="button" disabled={!valid} onClick={save} className="col-span-2 rounded border border-border px-3 py-1 text-sm disabled:opacity-50">
        Add debt
      </button>
      {err && <p role="alert" className="col-span-2 text-sm text-danger">{err}</p>}
    </div>
  )
}

function ImportDrop({ onDone }: { onDone: () => void }) {
  const [institution, setInstitution] = useState<Institution>('Apple Card')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const send = async (file: File | undefined) => {
    if (!file) return
    try {
      const r = await financeApi.importStatement(file, institution)
      setMsg(r.duplicate ? 'Already imported, nothing added.' : `Imported ${r.added} of ${r.rows} rows.`)
      setErr(null)
      onDone()
    } catch (e) {
      setMsg(null)
      setErr(e instanceof Error ? e.message : 'Import failed')
    }
  }
  const drop = (e: DragEvent) => {
    e.preventDefault()
    void send(e.dataTransfer.files[0])
  }
  return (
    <div>
      <label className="text-xs text-secondary">Statement from
        <select className="ml-2 rounded border border-border bg-transparent p-1 text-sm" value={institution}
          onChange={(e) => setInstitution(e.target.value as Institution)}>
          {INSTITUTIONS.map((i) => <option key={i}>{i}</option>)}
        </select>
      </label>
      <div
        data-testid="fin-dropzone"
        onDragOver={(e) => e.preventDefault()}
        onDrop={drop}
        className="mt-2 rounded border border-dashed border-border p-4 text-center text-sm text-secondary"
      >
        Drop a CSV, OFX or QFX file here, or
        <input aria-label="Statement file" type="file" accept=".csv,.ofx,.qfx" className="ml-2 text-xs"
          onChange={(e) => void send(e.target.files?.[0])} />
      </div>
      {msg && <p className="mt-2 text-sm text-secondary" data-testid="fin-import-msg">{msg}</p>}
      {err && <p role="alert" className="mt-2 text-sm text-danger">{err}</p>}
    </div>
  )
}

export default function FinanceView() {
  const [accounts, setAccounts] = useState<DebtAccount[]>([])
  const [plan, setPlan] = useState<Plan | null>(null)
  const [reminders, setReminders] = useState<Reminder[]>([])
  const [extra, setExtra] = useState('0')
  const [error, setError] = useState<string | null>(null)
  const extraCents = Math.max(0, Math.round(Number(extra || 0) * 100))

  const load = useCallback(() => {
    Promise.all([financeApi.accounts(), financeApi.plan(extraCents), financeApi.reminders()])
      .then(([a, p, r]) => {
        setAccounts(a)
        setPlan(p)
        setReminders(r)
        setError(null)
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [extraCents])
  useEffect(load, [load])

  const remove = async (id: string) => {
    await financeApi.deleteAccount(id)
    load()
  }

  return (
    <div className="space-y-6 p-4">
      <h1 className="text-lg font-semibold text-primary">Finance</h1>
      <p className="text-xs text-muted">
        Read-only. You enter debts and drop statement files; nothing here pays or moves money.
      </p>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}

      <section className="rounded border border-border p-4" data-testid="fin-reminders">
        <h2 className="text-sm font-semibold text-primary">Reminders</h2>
        {reminders.length === 0 ? (
          <p className="text-xs text-muted">Nothing due soon.</p>
        ) : (
          <ul className="mt-2 space-y-1 text-sm text-secondary">
            {reminders.map((r) => (
              <li key={`${r.kind}-${r.id}`} data-testid={`fin-reminder-${r.kind}`}>
                {r.title}: {r.overdue ? 'overdue since' : 'due'} {r.due_date} ({money(r.amount_cents)} {r.amount_label})
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="rounded border border-border p-4" data-testid="fin-accounts">
        <h2 className="text-sm font-semibold text-primary">Debts</h2>
        {accounts.length === 0 && <p className="text-xs text-muted">No debts entered yet.</p>}
        <ul className="mt-2 space-y-1 text-sm text-secondary">
          {accounts.map((a) => (
            <li key={a.id} className="flex items-center justify-between">
              <span>{a.institution} {a.name}: {money(a.balance_cents)} at {a.apr_pct}% APR, minimum {money(a.min_payment_cents)}</span>
              <button type="button" aria-label={`Remove ${a.name}`} onClick={() => void remove(a.id)}
                className="rounded border border-border px-2 text-xs">Remove</button>
            </li>
          ))}
        </ul>
        <AddAccount onAdded={load} />
      </section>

      <section className="rounded border border-border p-4" data-testid="fin-plan">
        <h2 className="text-sm font-semibold text-primary">Payoff plan</h2>
        <label className="text-xs text-secondary">Extra per month ($)
          <input aria-label="Extra per month" type="number" min="0" step="1" value={extra}
            onChange={(e) => setExtra(e.target.value)}
            className="ml-2 w-24 rounded border border-border bg-transparent p-1 text-sm" />
        </label>
        {plan && plan.total_balance_cents > 0 ? (
          <div className="mt-3 space-y-2 text-sm text-secondary">
            <PlanChart plan={plan} />
            <p data-testid="fin-plan-minimums">Minimums only: {describe(plan.minimums)}</p>
            <p data-testid="fin-plan-avalanche">Avalanche (highest APR first): {describe(plan.avalanche)}
              {plan.avalanche.interest_saved_cents != null && `, saves ${money(plan.avalanche.interest_saved_cents)}`}</p>
            <p data-testid="fin-plan-snowball">Snowball (smallest balance first): {describe(plan.snowball)}
              {plan.snowball.interest_saved_cents != null && `, saves ${money(plan.snowball.interest_saved_cents)}`}</p>
          </div>
        ) : (
          <p className="mt-2 text-xs text-muted">Add a debt to see a plan.</p>
        )}
      </section>

      <section className="rounded border border-border p-4" data-testid="fin-import">
        <h2 className="text-sm font-semibold text-primary">Import a statement</h2>
        <ImportDrop onDone={load} />
      </section>
    </div>
  )
}
