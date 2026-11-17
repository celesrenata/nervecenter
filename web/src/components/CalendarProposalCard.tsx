import { useMemo, useState } from 'react'
import {
  CalendarWriteError,
  cancelProposal,
  confirmProposal,
  undoEvent,
  type CalendarProposal,
} from '../api/calendar'
import { connectionsApi } from '../api/connections'

function fmtLocal(iso: string, tz: string, allDay?: boolean): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const opts: Intl.DateTimeFormatOptions = allDay
    ? { weekday: 'short', month: 'short', day: 'numeric', timeZone: tz }
    : {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        timeZone: tz,
        timeZoneName: 'short',
      }
  try {
    return d.toLocaleString(undefined, opts)
  } catch {
    return d.toLocaleString()
  }
}

type CardState =
  | { kind: 'idle' }
  | { kind: 'busy' }
  | { kind: 'done'; account: string }
  | { kind: 'cancelled' }

/** Specific, user-facing text for a refused confirm/cancel. */
function describeWriteError(err: unknown): string {
  if (err instanceof CalendarWriteError) {
    if (err.code === 'proposal_not_found') return 'This proposal expired. Ask again to get a new one.'
    if (err.code === 'invalid_time') return `The time is not valid: ${err.message}`
    if (err.code === 'account_not_writable') return 'That account cannot be written to. Pick another account.'
    return `Could not add the event (${err.code}): ${err.message}`
  }
  if (err instanceof TypeError) return 'Could not reach the server. Try again.'
  return err instanceof Error ? err.message : 'Could not add the event.'
}

export function CalendarProposalCard({ proposal }: { proposal: CalendarProposal }) {
  const accounts = useMemo(() => {
    const list = proposal.accounts.filter((a) => a.connection_id)
    if (!list.some((a) => a.id === proposal.account.id)) {
      list.unshift({ ...proposal.account, connection_id: null })
    }
    return list
  }, [proposal])
  const [accountId, setAccountId] = useState(proposal.account.id)
  const [state, setState] = useState<CardState>({ kind: 'idle' })
  const [error, setError] = useState<string | null>(null)
  const [consent, setConsent] = useState<{ connectionId: string } | null>(null)

  const busy = state.kind === 'busy'
  const selected = accounts.find((a) => a.id === accountId)

  const confirm = async () => {
    setState({ kind: 'busy' })
    setError(null)
    try {
      await confirmProposal(proposal.id, { account_id: accountId })
      setConsent(null)
      setState({ kind: 'done', account: selected?.label ?? proposal.account.label })
    } catch (err) {
      setState({ kind: 'idle' })
      if (err instanceof CalendarWriteError && err.code === 'consent_required') {
        const connectionId = err.connectionId ?? selected?.connection_id ?? null
        if (connectionId) {
          setConsent({ connectionId })
          return
        }
      }
      setError(describeWriteError(err))
    }
  }

  const cancel = async () => {
    setState({ kind: 'busy' })
    setError(null)
    try {
      await cancelProposal(proposal.id)
      setState({ kind: 'cancelled' })
    } catch (err) {
      setState({ kind: 'idle' })
      setError(describeWriteError(err))
    }
  }

  const grant = async () => {
    if (!consent) return
    setError(null)
    try {
      const res = await connectionsApi.startAuth(consent.connectionId, { extra: ['calendar_write'] })
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
      data-testid="calendar-proposal"
      className="mt-3 rounded-lg border border-line bg-elevated p-3 text-primary space-y-2"
    >
      <p className="text-sm font-semibold">{proposal.title}</p>
      <p className="text-xs">
        {fmtLocal(proposal.start, proposal.tz, proposal.all_day)} –{' '}
        {fmtLocal(proposal.end, proposal.tz, proposal.all_day)}{' '}
        <span className="text-muted">({proposal.tz})</span>
      </p>
      {proposal.location && <p className="text-xs text-muted">{proposal.location}</p>}

      {state.kind === 'idle' || state.kind === 'busy' ? (
        <>
          <label className="block text-xs">
            <span className="text-muted">Calendar account</span>
            <select
              data-testid="cal-account"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              disabled={busy}
              className="mt-1 w-full px-2 py-1.5 rounded-md border border-line bg-hover/40 text-xs text-primary"
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.label}
                  {a.can_write ? '' : ' (needs write permission)'}
                </option>
              ))}
            </select>
          </label>

          {proposal.assumptions.length > 0 && (
            <ul data-testid="cal-assumptions" className="list-disc pl-5 text-xs text-muted">
              {proposal.assumptions.map((a, i) => (
                <li key={`a${i}`}>{a}</li>
              ))}
            </ul>
          )}
          {proposal.warnings.length > 0 && (
            <ul
              data-testid="cal-warnings"
              className="list-disc pl-5 text-xs text-amber-700 dark:text-amber-300"
            >
              {proposal.warnings.map((w, i) => (
                <li key={`w${i}`}>{w}</li>
              ))}
            </ul>
          )}
          {(proposal.conflicts?.length ?? 0) > 0 && (
            <p className="text-xs text-amber-700 dark:text-amber-300">
              Overlaps: {proposal.conflicts!.map((c) => c.title).join(', ')}
            </p>
          )}

          {consent && (
            <div
              role="alert"
              data-testid="cal-consent"
              className="rounded-md border border-line bg-hover/40 p-2 text-xs space-y-2"
            >
              <p>Google needs permission to write to this calendar</p>
              <button
                type="button"
                data-testid="cal-grant"
                onClick={() => void grant()}
                className="rounded border border-line px-2.5 py-1 text-xs font-medium"
              >
                Grant calendar write access
              </button>
            </div>
          )}
          {error && (
            <p role="alert" data-testid="cal-error" className="text-xs text-red-600 dark:text-red-400">
              {error}
            </p>
          )}

          <div className="flex gap-2">
            <button
              type="button"
              data-testid="cal-confirm"
              onClick={() => void confirm()}
              disabled={busy}
              className="bg-blue-500 hover:bg-blue-600 text-white px-3 py-1 rounded-md text-xs disabled:opacity-50"
            >
              {busy ? 'Adding…' : 'Confirm'}
            </button>
            <button
              type="button"
              data-testid="cal-cancel"
              onClick={() => void cancel()}
              disabled={busy}
              className="border border-line px-3 py-1 rounded-md text-xs disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </>
      ) : state.kind === 'done' ? (
        <p data-testid="cal-result" className="text-xs font-medium text-green-700 dark:text-green-400">
          Added to {state.account}
        </p>
      ) : (
        <p data-testid="cal-result" className="text-xs text-muted">
          Cancelled. Nothing was added.
        </p>
      )}
    </div>
  )
}

/** Undo for an event Ask created directly (NERVE_ASK_CALENDAR_CONFIRM_FIRST=false). */
export function CreatedEventUndo({ eventId }: { eventId: string }) {
  const [state, setState] = useState<'idle' | 'busy' | 'undone'>('idle')
  const [error, setError] = useState<string | null>(null)
  const undo = async () => {
    setState('busy')
    setError(null)
    try {
      await undoEvent(eventId)
      setState('undone')
    } catch (err) {
      setState('idle')
      setError(
        err instanceof CalendarWriteError && err.status === 404
          ? 'Undo is no longer available for this event.'
          : describeWriteError(err),
      )
    }
  }
  if (state === 'undone') {
    return <p data-testid="cal-undone" className="mt-2 text-xs">Removed from your calendar.</p>
  }
  return (
    <div className="mt-2 space-y-1">
      <button
        type="button"
        data-testid="cal-undo"
        onClick={() => void undo()}
        disabled={state === 'busy'}
        className="rounded border border-line px-2.5 py-1 text-xs font-medium disabled:opacity-50"
      >
        {state === 'busy' ? 'Undoing…' : 'Undo'}
      </button>
      {error && (
        <p role="alert" data-testid="cal-undo-error" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  )
}
