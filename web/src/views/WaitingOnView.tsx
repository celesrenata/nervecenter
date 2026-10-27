import { useState, useEffect, useCallback } from 'react'
import { waitingOnApi, ApiError } from '../api/client'
import { AlertTriangleIcon } from '../components/icons'

/**
 * WaitingOnView — the Waiting-On plane (prompt2 §19 — Waiting-On Detection /
 * §31 "what am I waiting for?").
 *
 * Shows *outgoing requests that received no response*: you asked Sam for the
 * deployment credentials, 3 days pass with no reply, and NerveCenter surfaces
 * "WAITING ON / Sam / deployment credentials / 3 days / [Follow up]" (§19).
 * The relay / personal-ops layers detect the outgoing request and post a
 * normalized record to the API (channel-side detection is §46/§47 source-side,
 * out of scope); NerveCenter only tracks it here and **never sends anything**
 * — a follow-up produces a rule-based *draft* the human reviews and sends
 * (§13: "AI drafts, human sends").
 *
 * Each OPEN request produces exactly one live AttentionItem through the P0
 * attention engine, so it also surfaces on the Now screen with the
 * explainable factor "waiting on {person} for {description} ({n} days)".
 * Resolving a request (the counterpart answered / the matter closed)
 * resolves that AttentionItem, taking it off the Now screen.
 *
 * API failures surface as an `role="alert"` banner (OV-1 contract) rather
 * than a silent empty state; the no-open-requests case is an honest empty
 * state.
 */

export interface WaitingOnItem {
  request_id: string
  person_display_name: string
  person_id: string | null
  description: string
  requested_at: string
  source: string
  source_thread_id: string | null
  status: 'open' | 'resolved' | 'cancelled'
  attention_id: string | null
  follow_up_count: number
  last_follow_up_at: string | null
  resolved_at: string | null
  note: string | null
  created_at?: string | null
  updated_at?: string | null
  /** Optional escalation policy (P0 AttentionItem contract, §22/§45): e.g.
   * `{"priority": "urgent"}` → follow up within 24h, or escalate to a VIP
   * if unresolved. The UI shows a subtle "Urgent" marker for it. */
  escalation_policy?: Record<string, unknown> | null
}

/** True when the request carries the "urgent" escalation policy (§22/§45).
 * The backend posts `{"priority": "urgent"}`; we only key off that one
 * producer contract so unknown keys never render as urgent. */
function isUrgent(item: WaitingOnItem): boolean {
  return (
    item.status === 'open' &&
    typeof item.escalation_policy === 'object' &&
    item.escalation_policy !== null &&
    (item.escalation_policy as Record<string, unknown>).priority === 'urgent'
  )
}

interface FollowUpResult {
  request_id: string
  draft: string
  follow_up_count: number
  last_follow_up_at: string | null
  status: string
  draft_only: boolean
  basis: string
  new_attention: unknown
}

function formatApiError(err: unknown): string {
  const e = err as { status?: number; detail?: string; message?: string } | null
  if (e && typeof e.status === 'number' && typeof e.detail === 'string') {
    return `${e.status} — ${e.detail}`
  }
  if (e && typeof e.message === 'string' && e.message) return e.message
  return 'Request failed'
}

function formatWhen(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleDateString()
}

/** Whole days between `requested_at` and now (mirrors the backend rule). */
function daysElapsed(requestedAt: string, now = new Date()): number {
  const t = new Date(requestedAt).getTime()
  if (Number.isNaN(t)) return 0
  return Math.max(0, Math.floor((now.getTime() - t) / (24 * 60 * 60 * 1000)))
}

function formatDays(days: number): string {
  if (days <= 0) return 'today'
  if (days === 1) return '1 day'
  return `${days} days`
}

type Busy = { requestId: string; action: 'resolve' | 'follow-up' } | null

export function WaitingOnView() {
  const [items, setItems] = useState<WaitingOnItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showResolved, setShowResolved] = useState(false)
  const [busy, setBusy] = useState<Busy>(null)
  const [drafts, setDrafts] = useState<Record<string, FollowUpResult>>({})

  const load = useCallback(async () => {
    try {
      setError(null)
      const res = await waitingOnApi.list(showResolved ? 'all' : 'open')
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      setItems(res.data as WaitingOnItem[])
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setLoading(false)
    }
  }, [showResolved])

  useEffect(() => {
    void load()
  }, [load])

  const doFollowUp = useCallback(async (requestId: string) => {
    setBusy({ requestId, action: 'follow-up' })
    try {
      const res = await waitingOnApi.followUp(requestId)
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      setDrafts((prev) => ({ ...prev, [requestId]: res.data as FollowUpResult }))
      await load()
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setBusy(null)
    }
  }, [load])

  const doResolve = useCallback(async (requestId: string) => {
    setBusy({ requestId, action: 'resolve' })
    try {
      const res = await waitingOnApi.resolve(requestId)
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      await load()
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setBusy(null)
    }
  }, [load])

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-primary">Waiting On</h1>
          <p className="text-sm text-muted">
            {loading
              ? 'Loading…'
              : `${items.length} request${items.length === 1 ? '' : 's'} · outgoing asks with no response yet`}
          </p>
        </div>
        <label className="flex cursor-pointer items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
          <input
            type="checkbox"
            checked={showResolved}
            onChange={(e) => setShowResolved(e.target.checked)}
            className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
          />
          Show resolved
        </label>
      </div>

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/30 px-4 py-3 text-sm text-red-700 dark:text-red-300"
        >
          {error}
        </div>
      )}

      {loading ? (
        <div className="rounded-lg border border-line p-4 text-sm text-muted">
          Loading waiting-on requests…
        </div>
      ) : items.length === 0 ? (
        <div className="rounded-lg border border-line p-4 text-sm text-muted">
          {showResolved
            ? 'No waiting-on requests yet.'
            : 'Nothing waiting on you — no outgoing requests without a response yet. New ones land here from the Personal Relay.'}
        </div>
      ) : (
        <ul className="space-y-3">
          {items.map((item) => {
            const open = item.status === 'open'
            const days = daysElapsed(item.requested_at)
            const draft = drafts[item.request_id]
            const thisBusy = busy?.requestId === item.request_id ? busy.action : null
            const urgent = isUrgent(item)
            return (
              <li
                key={item.request_id}
                className={`rounded-lg border p-4 ${
                  open
                    ? 'border-amber-300 dark:border-amber-600/50 bg-amber-50/60 dark:bg-amber-900/20'
                    : 'border-line bg-elevated/40'
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-semibold uppercase tracking-wide text-muted">
                        {open ? 'Waiting on' : item.status}
                      </span>
                      {urgent && (
                        <span
                          className="inline-flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-700 dark:bg-red-900/40 dark:text-red-300"
                          aria-label="Urgent escalation policy"
                          title="Urgent: follow up within 24h, or escalate to a VIP if unresolved"
                          data-testid={`waiting-on-urgent-${item.request_id}`}
                        >
                          <AlertTriangleIcon size={12} />
                          Urgent
                        </span>
                      )}
                      <span
                        className="rounded-full bg-gray-200 px-2 py-0.5 text-xs font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                        title="Source channel"
                      >
                        {item.source}
                      </span>
                      {!open && item.resolved_at && (
                        <span className="text-xs text-muted">
                          resolved {formatWhen(item.resolved_at)}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-lg font-medium text-gray-900 dark:text-white">
                      {item.person_display_name}
                      <span className="text-gray-400 dark:text-gray-500"> · </span>
                      <span className="text-gray-700 dark:text-gray-200">{item.description}</span>
                    </p>
                    <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">
                      Asked {formatWhen(item.requested_at)}
                      {open && (
                        <>
                          {' '}— waiting{' '}
                          <span className="font-semibold text-amber-700 dark:text-amber-400">
                            {formatDays(days)}
                          </span>
                        </>
                      )}
                      {item.follow_up_count > 0 && (
                        <> · {item.follow_up_count} follow-up{item.follow_up_count === 1 ? '' : 's'}</>
                      )}
                    </p>
                    {item.note && (
                      <p className="mt-1 text-sm italic text-muted">
                        Note: {item.note}
                      </p>
                    )}
                  </div>

                  {open && (
                    <div className="flex shrink-0 gap-2">
                      <button
                        type="button"
                        onClick={() => doFollowUp(item.request_id)}
                        disabled={thisBusy != null}
                        className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:opacity-50"
                      >
                        {thisBusy === 'follow-up' ? 'Drafting…' : 'Follow up'}
                      </button>
                      <button
                        type="button"
                        onClick={() => doResolve(item.request_id)}
                        disabled={thisBusy != null}
                        className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
                      >
                        {thisBusy === 'resolve' ? 'Resolving…' : 'Resolved'}
                      </button>
                    </div>
                  )}
                </div>

                {draft && (
                  <div className="mt-3 rounded-md border border-blue-200 dark:border-blue-800 bg-blue-50/60 dark:bg-blue-900/20 p-3">
                    <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-blue-700 dark:text-blue-400">
                      <span>Draft follow-up (never sent — review & send yourself)</span>
                    </div>
                    <p className="mt-1 text-sm text-gray-800 dark:text-gray-100">{draft.draft}</p>
                    <p className="mt-1 text-xs text-muted">{draft.basis}</p>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
