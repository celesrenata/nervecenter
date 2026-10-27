import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { todayApi, type TodayResponse } from '../api/today'
import { connectionsApi, type Connection } from '../api/connections'
import { attentionApi } from '../api/client'
import { mapConnectionState } from '../components/connections/stateMapping'

/**
 * TodayView — the default landing page (spec §13, workstream L).
 *
 * Consumes `GET /api/today` (the frozen shape in `backend/api/endpoints/today.py`):
 * three sections — **Needs You** (top actionable items), **Coming Up**
 * (appointments / deadlines in the next 48h) and **Handled** (resolved /
 * completed in the last 24h) — plus a one-tap **Low Energy Mode** toggle
 * (`PATCH /api/today/mode`). While Low Energy Mode is on the backend already
 * truncates `needs_you` to a single item and sets `simplified: true`; this view
 * then renders the reduced layout with Done / Later / Help Me on that item
 * (Done → `POST /api/attention/{id}/resolve`, Later → `/snooze` +24h,
 * Help Me → the Ask page). Those actions only target attention items —
 * a lone commitment card has no buttons (honest: the wave-1 endpoints are
 * attention-scoped).
 *
 * Design constraints (frozen §13): no streaks, no guilt, no "missed routine"
 * counts — the response shape itself has none, and this view adds none.
 * The connection health strip at the bottom is read-only (one badge per
 * `GET /api/connections` row) and links to the Connections page.
 */

const URGENCY_LABEL: Record<string, string> = {
  low: 'Low',
  normal: 'Normal',
  high: 'High',
  critical: 'Critical',
}

const URGENCY_TONE: Record<string, string> = {
  low: 'bg-gray-100 dark:bg-gray-900/60 border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300',
  normal: 'bg-blue-100 dark:bg-blue-900/40 border-blue-300 dark:border-blue-700 text-blue-700 dark:text-blue-300',
  high: 'bg-amber-100 dark:bg-amber-900/40 border-amber-300 dark:border-amber-700 text-amber-700 dark:text-amber-300',
  critical: 'bg-red-100 dark:bg-red-900/40 border-red-300 dark:border-red-700 text-red-700 dark:text-red-300',
}

const HANDLED_STATE_LABEL: Record<string, string> = {
  resolved: 'Done',
  dismissed: 'Dismissed',
  snoozed: 'Later',
  done: 'Done',
}

/** Convert an ISO timestamp to a compact relative label, or null. */
function formatWhen(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const hours = (d.getTime() - Date.now()) / 3_600_000
  if (Math.abs(hours) < 1) return hours < 0 ? 'overdue' : 'now'
  if (Math.abs(hours) < 48) return hours < 0 ? `overdue ${Math.round(Math.abs(hours))}h` : `in ${Math.round(hours)}h`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/** Render an API failure as `<status> — <detail>` (OV-1 contract). */
function formatError(err: unknown): string {
  const e = err as { status?: number; detail?: string; message?: string } | null
  if (e && typeof e.status === 'number' && typeof e.detail === 'string') return `${e.status} — ${e.detail}`
  return e instanceof Error ? e.message : 'Request failed'
}

/** The single most important next action in Low Energy Mode. */
interface LowEnergyAction {
  id: string
  title: string
  urgency: string | null
  deadline_at: string | null
  recommended_action: string | null
  isAttention: boolean
}

export function TodayView() {
  const [data, setData] = useState<TodayResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [connections, setConnections] = useState<Connection[]>([])
  const [connError, setConnError] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setData(await todayApi.getToday())
      setError(null)
    } catch (err) {
      setError(formatError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // Read-only connection health strip — never blocks the page.
  useEffect(() => {
    let cancelled = false
    connectionsApi
      .list()
      .then((res) => {
        if (!cancelled) setConnections(res.data ?? [])
      })
      .catch((err) => {
        if (!cancelled) setConnError(formatError(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

  const toggleLowEnergy = async (on: boolean) => {
    let degraded = false
    try {
      const result = await todayApi.setLowEnergyMode(on)
      if (result.executed === false || result.low_energy !== on) {
        // Honest degradation: no durable store → the toggle is not persisted.
        degraded = true
      }
      await load()
      if (degraded) {
        // Set after the refresh so the success-path `setError(null)` does not
        // wipe the message before it renders.
        setError('Low Energy Mode is not available right now — no durable storage configured.')
      }
    } catch (err) {
      setError(formatError(err))
    }
  }

  const actionNow = useCallback(
    async (attentionId: string, action: 'resolve' | 'snooze') => {
      setBusyAction(action)
      try {
        // Done → POST /api/attention/{id}/resolve, Later → /snooze (house
        // convention: the in-memory attention client owns these routes).
        // The durable row then moves to `handled` on the next /api/today read.
        await attentionApi.action(attentionId, action)
        await load()
      } catch (err) {
        setError(formatError(err))
      } finally {
        setBusyAction(null)
      }
    },
    [load],
  )

  if (loading) {
    return (
      <div className="max-w-3xl mx-auto p-6">
        <p className="text-secondary text-sm">Loading today…</p>
      </div>
    )
  }

  const lowEnergy = Boolean(data?.low_energy)
  const top = lowEnergy ? (data?.needs_you[0] as (LowEnergyAction & { kind: string }) | undefined) : undefined

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-8">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-primary">Today</h1>
          <p className="text-sm text-muted mt-1">
            What needs you, what is coming up, what is already handled.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={lowEnergy}
          disabled={busyAction !== null}
          onClick={() => void toggleLowEnergy(!lowEnergy)}
          className={`inline-flex items-center gap-2 px-3 py-2 rounded-lg border text-sm font-medium transition-colors ${
            lowEnergy
              ? 'bg-accent-soft border-accent text-accent-contrast'
              : 'bg-elevated border-line text-secondary hover:bg-hover'
          }`}
        >
          <span aria-hidden="true">{lowEnergy ? '●' : '○'}</span>
          Low Energy Mode
        </button>
      </header>

      {error && (
        <div
          role="alert"
          className="text-red-700 dark:text-red-300 bg-red-100 dark:bg-red-900/40 border border-red-300 dark:border-red-700 p-4 rounded-lg"
        >
          <p className="text-sm">{error}</p>
        </div>
      )}

      {data?.low_energy && top && (
        /* ── Reduced layout: exactly one next action ───────────────────── */
        <section aria-labelledby="today-low-energy" className="bg-elevated rounded-lg shadow-lg p-6">
          <h2 id="today-low-energy" className="text-lg font-semibold text-primary">
            One thing at a time
          </h2>
          <div className="mt-4 space-y-2">
            <p className="text-base font-medium text-primary">{top.title}</p>
            {(top.urgency || top.deadline_at) && (
              <p className="text-sm text-secondary">
                {[
                  top.urgency && top.urgency !== 'normal' ? URGENCY_LABEL[top.urgency] : null,
                  formatWhen(top.deadline_at),
                ]
                  .filter(Boolean)
                  .join(' · ')}
              </p>
            )}
            {top.recommended_action && (
              <p className="text-sm text-secondary">{top.recommended_action}</p>
            )}
          </div>
          {top.kind === 'attention' && (
            <div className="mt-4 flex gap-2">
              <button
                type="button"
                disabled={busyAction !== null}
                onClick={() => void actionNow(top.id, 'resolve')}
                className="px-4 py-2 rounded-lg bg-accent text-accent-contrast text-sm font-medium hover:opacity-90 disabled:opacity-50"
              >
                Done
              </button>
              <button
                type="button"
                disabled={busyAction !== null}
                onClick={() => void actionNow(top.id, 'snooze')}
                className="px-4 py-2 rounded-lg border border-line bg-elevated text-secondary text-sm font-medium hover:bg-hover disabled:opacity-50"
              >
                Later
              </button>
              <Link
                to="/ask"
                className="px-4 py-2 rounded-lg border border-line bg-elevated text-secondary text-sm font-medium hover:bg-hover"
              >
                Help Me
              </Link>
            </div>
          )}
        </section>
      )}

      {!lowEnergy && (
        <>
          {/* ── Needs You ─────────────────────────────────────────────────── */}
          <section aria-labelledby="today-needs-you" className="space-y-3">
            <h2 id="today-needs-you" className="text-lg font-semibold text-primary">
              Needs You
            </h2>
            {!data || data.needs_you.length === 0 ? (
              <div className="bg-elevated p-6 rounded-lg shadow-lg">
                <p className="text-sm text-secondary">Nothing needs you right now.</p>
              </div>
            ) : (
              <ul className="space-y-3">
                {data.needs_you.map((item) => (
                  <li
                    key={item.id}
                    className="bg-elevated p-5 rounded-lg shadow-lg"
                    data-testid={`today-needs-you-${item.id}`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-base font-medium text-primary">{item.title}</p>
                        {item.body && <p className="text-sm text-secondary mt-1">{item.body}</p>}
                        {item.recommended_action && (
                          <p className="text-sm text-secondary mt-1">{item.recommended_action}</p>
                        )}
                      </div>
                      {item.urgency && (
                        <span
                          className={`shrink-0 text-xs font-medium px-2 py-0.5 rounded-full border ${
                            URGENCY_TONE[item.urgency] ?? URGENCY_TONE.normal
                          }`}
                        >
                          {URGENCY_LABEL[item.urgency] ?? item.urgency}
                        </span>
                      )}
                    </div>
                    <div className="mt-2 flex items-center gap-2 text-xs text-muted">
                      <span>{item.kind === 'attention' ? 'Attention' : 'Commitment'}</span>
                      {formatWhen(item.deadline_at) && <span>· {formatWhen(item.deadline_at)}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* ── Coming Up ─────────────────────────────────────────────────── */}
          <section aria-labelledby="today-coming-up" className="space-y-3">
            <h2 id="today-coming-up" className="text-lg font-semibold text-primary">
              Coming Up
            </h2>
            {!data || data.coming_up.length === 0 ? (
              <div className="bg-elevated p-6 rounded-lg shadow-lg">
                <p className="text-sm text-secondary">Nothing scheduled in the next 48 hours.</p>
              </div>
            ) : (
              <ul className="space-y-2">
                {data.coming_up.map((item) => (
                  <li
                    key={item.id}
                    className="bg-elevated px-4 py-3 rounded-lg shadow-sm flex items-center justify-between gap-3"
                    data-testid={`today-coming-up-${item.id}`}
                  >
                    <span className="text-sm text-primary">{item.title}</span>
                    <span className="text-xs text-muted shrink-0">
                      {formatWhen(item.deadline_at) ??
                        (item.kind === 'calendar' ? 'Today' : 'Soon')}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* ── Handled ───────────────────────────────────────────────────── */}
          <section aria-labelledby="today-handled" className="space-y-3">
            <h2 id="today-handled" className="text-lg font-semibold text-primary">
              Handled
            </h2>
            {!data || data.handled.length === 0 ? (
              <div className="bg-elevated p-6 rounded-lg shadow-lg">
                <p className="text-sm text-secondary">Nothing handled in the last 24 hours.</p>
              </div>
            ) : (
              <ul className="space-y-2">
                {data.handled.map((item) => (
                  <li
                    key={item.id}
                    className="px-4 py-2 flex items-center justify-between gap-3 text-sm"
                    data-testid={`today-handled-${item.id}`}
                  >
                    <span className="text-secondary">{item.title}</span>
                    <span className="text-xs text-muted shrink-0">
                      {HANDLED_STATE_LABEL[item.state] ?? item.state}
                      {item.at ? ` · ${formatWhen(item.at)}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}

      {/* ── Connection health strip (read-only) ──────────────────────────── */}
      <section aria-labelledby="today-connections" className="space-y-3 border-t border-line pt-6">
        <div className="flex items-center justify-between gap-3">
          <h2 id="today-connections" className="text-sm font-semibold text-primary">
            Connections
          </h2>
          <Link to="/connections" className="text-sm text-secondary hover:text-primary">
            Manage →
          </Link>
        </div>
        {connError ? (
          <p className="text-sm text-secondary">Connection status unavailable.</p>
        ) : connections.length === 0 ? (
          <p className="text-sm text-secondary">
            No connections configured yet.{' '}
            <Link to="/connections" className="underline">
              Set one up
            </Link>
            .
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {connections.map((conn) => {
              const pres = mapConnectionState({
                state: conn.state,
                auth_status: conn.auth_status,
                operational_status: conn.operational_status,
                last_error_code: conn.last_error_code,
                last_success_at: conn.last_success_at,
              })
              return (
                <Link
                  key={conn.id}
                  to="/connections"
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-xs font-medium ${
                    pres.stateTone === 'success'
                      ? 'bg-green-100 dark:bg-green-900/40 border-green-300 dark:border-green-700 text-green-800 dark:text-green-200'
                      : pres.stateTone === 'warning'
                        ? 'bg-amber-100 dark:bg-amber-900/40 border-amber-300 dark:border-amber-700 text-amber-800 dark:text-amber-200'
                        : pres.stateTone === 'danger'
                          ? 'bg-red-100 dark:bg-red-900/40 border-red-300 dark:border-red-700 text-red-800 dark:text-red-200'
                          : 'bg-gray-100 dark:bg-gray-900/60 border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300'
                  }`}
                  title={`${conn.label || conn.kind}: ${pres.label} · ${pres.operational.label}`}
                >
                  <span aria-hidden="true">{pres.stateTone === 'success' ? '●' : pres.stateTone === 'neutral' ? '○' : '!'}</span>
                  {conn.label || conn.kind}
                </Link>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}

export default TodayView
