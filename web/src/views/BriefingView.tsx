import { useState, useEffect, useCallback } from 'react'
import { briefingApi, ApiError, type Briefing, type BriefingKind } from '../api/client'
import { useSettings } from '../theme/SettingsContext'

/**
 * BriefingView — the Briefing plane (prompt2 §30 — Briefings).
 *
 * A read-only, one-screen projection of the day: the four sections §30's
 * "Morning Brief" calls out — **Actions** (things that need action, from the
 * top of the Now queue), **Waiting On** (open requests, longest-waiting
 * first), **Today** (calendar events overlapping the day), and **Overnight**
 * (what landed while awake-window closed: 24-hour activity counts). The
 * backend composes these from already-committed plane state; this view owns
 * no state of its kind other than the selected `kind` and the in-flight
 * `generated_at` snapshot.
 *
 * The §30 output shape is identical across kinds — the kind (morning /
 * midday / evening) is echoed as metadata, not a filter — so the selector
 * merely re-fetches; any section content that appears is the same four lines
 * regardless of which of the three briefs is being read.
 *
 * Honest empty state (§30 closing): when no section has content the view
 * renders "Nothing else requires attention." rather than inventing filler.
 * API failures surface as an `role="alert"` banner (OV-1 contract).
 */

const KINDS: BriefingKind[] = ['morning', 'midday', 'evening']

function formatApiError(err: unknown): string {
  const e = err as { status?: number; detail?: string; message?: string } | null
  if (e && typeof e.status === 'number' && typeof e.detail === 'string') {
    return `${e.status} — ${e.detail}`
  }
  if (e && typeof e.message === 'string') return e.message
  if (e && typeof (e as unknown as { status: number }).status === 'number') {
    return `HTTP ${(e as unknown as { status: number }).status}`
  }
  return String(err)
}

/** Format an ISO timestamp as a short wall-clock string (e.g. "6:30 PM"). */
function formatTime(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleString([], { hour: 'numeric', minute: '2-digit' })
}

/** "morning" -> "Morning" for the subtitle label. */
function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

const cardBase = 'bg-elevated rounded-lg border border-line p-5'

export function BriefingView() {
  const { settings } = useSettings()
  const [kind, setKind] = useState<BriefingKind>(settings.defaultBriefingKind)
  const [briefing, setBriefing] = useState<Briefing | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (k: BriefingKind) => {
    setLoading(true)
    setError(null)
    try {
      const res = await briefingApi.compose(k)
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      setBriefing(res.data)
    } catch (err) {
      setError(formatApiError(err))
      setBriefing(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(kind)
  }, [kind, load])

  const empty = briefing?.empty ?? false

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-primary">Briefing</h1>
          <p className="text-sm text-muted">
            {loading
              ? 'Composing…'
              : briefing
                ? `${capitalize(briefing.kind)} brief — composed ${formatTime(briefing.generated_at) ?? '—'}`
                : ''}
          </p>
        </div>
        <label className="flex cursor-pointer items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
          <span className="text-xs uppercase tracking-wide text-muted">Time of day</span>
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as BriefingKind)}
            className="rounded border border-gray-300 dark:border-gray-600 bg-elevated px-2 py-1 text-sm text-gray-700 dark:text-gray-200"
            data-testid="briefing-kind"
          >
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k.charAt(0).toUpperCase() + k.slice(1)}
              </option>
            ))}
          </select>
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

      {loading || !briefing ? (
        <div className="rounded-lg border border-line p-4 text-sm text-muted">
          Loading briefing…
        </div>
      ) : empty ? (
        <div
          className={cardBase}
          data-testid="briefing-empty"
        >
          <p className="text-gray-700 dark:text-gray-200">
            Nothing else requires attention.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          <Section title="Actions" count={briefing.actions.length} emptyText="Nothing needs action right now.">
            <ul className="space-y-2">
              {briefing.actions.map((a) => (
                <li
                  key={a.attention_id}
                  className="flex items-baseline justify-between gap-3 rounded border border-line bg-gray-50 dark:bg-gray-900/40 px-3 py-2"
                  data-testid={`briefing-action-${a.attention_id}`}
                >
                  <span className="min-w-0 break-words text-sm text-gray-900 dark:text-white">{a.label}</span>
                  {a.due && (
                    <span
                      className="shrink-0 text-xs font-medium text-muted"
                      data-testid={`briefing-action-due-${a.attention_id}`}
                    >
                      before {formatTime(a.due)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </Section>

          <Section title="Waiting On" count={briefing.waiting_on.length} emptyText="Nothing pending.">
            <ul className="space-y-2">
              {briefing.waiting_on.map((w) => (
                <li
                  key={w.request_id}
                  className="flex items-baseline justify-between gap-3 rounded border border-amber-200 dark:border-amber-600/50 bg-amber-50 dark:bg-amber-900/15 px-3 py-2"
                  data-testid={`briefing-waiting-${w.request_id}`}
                >
                  <span className="min-w-0 break-words text-sm text-gray-900 dark:text-white">
                    <span className="font-medium">{w.person}</span> — {w.description}
                  </span>
                  <span
                    className="shrink-0 rounded-full bg-amber-100 dark:bg-amber-800/40 px-2 py-0.5 text-xs font-medium text-amber-800 dark:text-amber-200"
                    data-testid={`briefing-waiting-days-${w.request_id}`}
                  >
                    {w.days_elapsed} day{w.days_elapsed === 1 ? '' : 's'}
                  </span>
                </li>
              ))}
            </ul>
          </Section>

          <Section title="Today" count={briefing.today.length} emptyText="Nothing on the calendar today.">
            <ul className="space-y-2">
              {briefing.today.map((ev) => (
                <li
                  key={ev.event_id}
                  className="flex items-baseline justify-between gap-3 rounded border border-line bg-gray-50 dark:bg-gray-900/40 px-3 py-2"
                  data-testid={`briefing-today-${ev.event_id}`}
                >
                  <span className="min-w-0 break-words text-sm text-gray-900 dark:text-white">{ev.title}</span>
                  {ev.start && (
                    <span className="shrink-0 text-xs font-medium text-muted">
                      {formatTime(ev.start)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </Section>

          <section className={cardBase} data-testid="briefing-overnight">
            <SectionHeader title="Overnight" count={undefined}>
              <span className="text-xs text-muted">in the last 24h</span>
            </SectionHeader>
            <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm text-gray-800 dark:text-gray-200">
              <span>
                <span className="font-semibold">{briefing.overnight.emails}</span> email
                {briefing.overnight.emails === 1 ? '' : 's'}
              </span>
              <span>
                <span className="font-semibold">{briefing.overnight.messages}</span> message
                {briefing.overnight.messages === 1 ? '' : 's'}
              </span>
              <span>
                <span className="font-semibold">{briefing.overnight.voicemails}</span> voicemail
                {briefing.overnight.voicemails === 1 ? '' : 's'}
              </span>
            </div>
          </section>
        </div>
      )}
    </div>
  )
}

/**
 * One section of the brief: a heading with an optional count, an optional
 * meta line, and either children (the list) or a muted empty-text line.
 */
function Section({
  title,
  count,
  emptyText,
  children,
}: {
  title: string
  count?: number
  emptyText?: string
  children: React.ReactNode
}) {
  const isEmpty = (typeof count === 'number' ? count : 0) === 0
  return (
    <section className={cardBase} data-testid={`briefing-section-${title.toLowerCase()}`}>
      <SectionHeader title={title} count={count} />
      {isEmpty ? (
        <p className="mt-3 text-sm text-muted">{emptyText ?? 'Nothing to show.'}</p>
      ) : (
        <div className="mt-3">{children}</div>
      )}
    </section>
  )
}

function SectionHeader({
  title,
  count,
  children,
}: {
  title: string
  count?: number
  children?: React.ReactNode
}) {
  return (
    <div className="flex items-baseline justify-between">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">{title}</h2>
      <div className="flex items-baseline gap-2">
        {typeof count === 'number' && (
          <span className="text-xs tabular-nums text-gray-400 dark:text-gray-500">{count}</span>
        )}
        {children}
      </div>
    </div>
  )
}
