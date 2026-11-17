import { useState } from 'react'
import {
  CalendarWriteError,
  cancelDeletion,
  confirmDeletion,
  type CalendarDeletionProposal,
  type DeletionConfirmResult,
  type DeletionRow,
} from '../api/calendar'
import { connectionsApi } from '../api/connections'

function fmt(iso: string | null, allDay?: boolean): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(
    undefined,
    allDay
      ? { weekday: 'short', month: 'short', day: 'numeric' }
      : { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' },
  )
}

function describeError(err: unknown): string {
  if (err instanceof CalendarWriteError) {
    if (err.code === 'proposal_not_found') return 'This list expired. Ask again to get a new one.'
    if (err.code === 'scope_required') return 'Choose whether to remove only the next one or all future ones.'
    return `Could not delete (${err.code}): ${err.message}`
  }
  if (err instanceof TypeError) return 'Could not reach the server. Try again.'
  return err instanceof Error ? err.message : 'Could not delete.'
}

const STATUS_TEXT: Record<string, string> = {
  deleted: 'Deleted',
  series_ended: 'Series ended as of today (past kept)',
  instances_deleted: 'Upcoming occurrences deleted',
  not_found: 'Already gone',
  failed: 'Failed',
}

function Row({
  row,
  checked,
  disabled,
  onToggle,
}: {
  row: DeletionRow
  checked: boolean
  disabled: boolean
  onToggle: () => void
}) {
  return (
    <li data-testid="cal-delete-row" className="flex gap-2 text-xs">
      <input
        type="checkbox"
        data-testid="cal-delete-check"
        aria-label={`Delete ${row.title}`}
        checked={checked}
        disabled={disabled}
        onChange={onToggle}
        className="mt-0.5"
      />
      <div className="space-y-0.5">
        <p className="font-medium">
          {row.title} <span className="text-muted">· {row.account.label}</span>
        </p>
        {row.kind === 'single' ? (
          <p>{fmt(row.start, row.all_day)}</p>
        ) : (
          <p>
            Series, {row.repeats ?? 'repeats'}. Next:{' '}
            {row.next_dates.filter(Boolean).map((d) => fmt(d, row.all_day)).join('; ')}.{' '}
            {row.upcoming_capped ? `At least ${row.upcoming_count}` : row.upcoming_count} upcoming.
          </p>
        )}
        <p className="text-muted">{row.path_label}</p>
      </div>
    </li>
  )
}

export function CalendarDeletionCard({ proposal }: { proposal: CalendarDeletionProposal }) {
  const [checked, setChecked] = useState<Set<string>>(
    () => new Set(proposal.rows.filter((r) => r.selected).map((r) => r.id)),
  )
  const [scope, setScope] = useState<string | null>(proposal.scope === 'ambiguous' ? null : proposal.scope)
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'cancelled'>('idle')
  const [result, setResult] = useState<DeletionConfirmResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [consent, setConsent] = useState<string | null>(null)
  const busy = state === 'busy'
  const strong = proposal.rows.filter((r) => r.confidence === 'strong')
  const unsure = proposal.rows.filter((r) => r.confidence === 'possible')
  const needScope = proposal.scope === 'ambiguous' && !scope

  const toggle = (id: string) =>
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const confirm = async () => {
    setState('busy')
    setError(null)
    try {
      const res = await confirmDeletion(proposal.id, {
        item_ids: proposal.rows.filter((r) => checked.has(r.id)).map((r) => r.id),
        scope,
      })
      setResult(res)
      setConsent(null)
      setState('done')
    } catch (err) {
      setState('idle')
      if (err instanceof CalendarWriteError && err.code === 'consent_required' && err.connectionId) {
        setConsent(err.connectionId)
        return
      }
      setError(describeError(err))
    }
  }

  const cancel = async () => {
    setState('busy')
    setError(null)
    try {
      await cancelDeletion(proposal.id)
      setState('cancelled')
    } catch (err) {
      setState('idle')
      setError(describeError(err))
    }
  }

  const grant = async () => {
    if (!consent) return
    try {
      const res = await connectionsApi.startAuth(consent, { extra: ['calendar_write'] })
      if (!('authorization_url' in res)) {
        setError('Google sign-in did not start. Open Connections and re-authorize Google.')
        return
      }
      window.sessionStorage.setItem('nerve.oauth.returnTo', '/ask')
      window.location.assign(res.authorization_url)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Google sign-in did not start.')
    }
  }

  return (
    <div
      data-testid="calendar-deletion"
      className="mt-3 rounded-lg border border-line bg-elevated p-3 text-primary space-y-2"
    >
      <p className="text-sm font-semibold">Remove calendar entries with {proposal.person}</p>
      {state === 'done' && result ? (
        <div data-testid="cal-delete-result" className="space-y-1 text-xs">
          <p className="font-medium text-green-700 dark:text-green-400">{result.summary}</p>
          <ul className="space-y-1">
            {result.results.map((r) => (
              <li key={r.id}>
                <span className="font-medium">{r.title}</span>: {STATUS_TEXT[r.status] ?? r.status}.{' '}
                <span className="text-muted">{r.detail}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : state === 'cancelled' ? (
        <p data-testid="cal-delete-result" className="text-xs text-muted">
          Cancelled. Nothing was deleted.
        </p>
      ) : (
        <>
          {proposal.scope === 'ambiguous' && (
            <fieldset className="text-xs space-y-1">
              <legend className="text-muted">How many?</legend>
              {proposal.scope_choices.map((c) => (
                <label key={c.id} className="flex items-center gap-2">
                  <input
                    type="radio"
                    name={`scope-${proposal.id}`}
                    data-testid={`cal-delete-scope-${c.id}`}
                    checked={scope === c.id}
                    disabled={busy}
                    onChange={() => setScope(c.id)}
                  />
                  {c.label}
                </label>
              ))}
            </fieldset>
          )}
          <ul className="space-y-2">
            {strong.map((r) => (
              <Row key={r.id} row={r} checked={checked.has(r.id)} disabled={busy} onToggle={() => toggle(r.id)} />
            ))}
          </ul>
          {unsure.length > 0 && (
            <div data-testid="cal-delete-unsure" className="space-y-2">
              <p className="text-xs font-semibold text-amber-700 dark:text-amber-300">
                Not sure (unchecked)
              </p>
              <ul className="space-y-2">
                {unsure.map((r) => (
                  <Row key={r.id} row={r} checked={checked.has(r.id)} disabled={busy} onToggle={() => toggle(r.id)} />
                ))}
              </ul>
            </div>
          )}
          {consent && (
            <div role="alert" data-testid="cal-delete-consent" className="rounded-md border border-line bg-hover/40 p-2 text-xs space-y-2">
              <p>Google needs permission to change this calendar</p>
              <button
                type="button"
                data-testid="cal-delete-grant"
                onClick={() => void grant()}
                className="rounded border border-line px-2.5 py-1 text-xs font-medium"
              >
                Grant calendar write access
              </button>
            </div>
          )}
          {error && (
            <p role="alert" data-testid="cal-delete-error" className="text-xs text-red-600 dark:text-red-400">
              {error}
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              data-testid="cal-delete-confirm"
              onClick={() => void confirm()}
              disabled={busy || checked.size === 0 || needScope}
              className="bg-red-600 hover:bg-red-700 text-white px-3 py-1 rounded-md text-xs disabled:opacity-50"
            >
              {busy ? 'Deleting…' : `Confirm delete (${checked.size})`}
            </button>
            <button
              type="button"
              data-testid="cal-delete-cancel"
              onClick={() => void cancel()}
              disabled={busy}
              className="border border-line px-3 py-1 rounded-md text-xs disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </>
      )}
    </div>
  )
}
