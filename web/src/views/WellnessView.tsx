import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  wellnessApi,
  type CheckInPayload,
  type CorrelationBlock,
  type SleepConsistency,
  type TrendBlock,
  type WellnessEntry,
  type WellnessStatus,
  type WellnessTrends,
} from '../api/wellness'

/**
 * WellnessView — the private Wellness Journal (Personal-Ops Recovery §10).
 *
 * A user-controlled check-in surface: mood, energy, stress, optional
 * journal and custom observations, one check-in per day. Trends are
 * deterministic statistics computed in NerveCenter (means, slope, one
 * sleep↔mood correlation) — **never a diagnosis**; the copy describes data
 * only. Sleep duration/consistency comes from workstream H's sleep data
 * when present, degrading to a "no sleep data" state otherwise.
 *
 * Privacy: wellness data stays in NerveCenter and is never sent to other
 * services or models. Wellness deliberately has no Today section and no
 * external-model path — this view is the only surface it appears on.
 * Controls: enable/disable, pause window, explicit clinician export
 * (audited), and a hard delete-all with typed "DELETE" confirmation.
 */

// ─── helpers ────────────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })
}

function fmtDuration(minutes: number | null | undefined): string {
  if (minutes == null) return '—'
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

const MOOD_LABELS: Record<number, string> = {
  1: 'rough',
  2: 'low',
  3: 'okay',
  4: 'good',
  5: 'great',
}

function directionWord(direction: TrendBlock['direction']): { text: string; color: string } {
  switch (direction) {
    case 'upward':
      return { text: '↑ upward', color: 'text-success' }
    case 'downward':
      return { text: '↓ downward', color: 'text-danger' }
    case 'flat':
      return { text: '→ steady', color: 'text-muted' }
    default:
      return { text: 'insufficient data', color: 'text-muted' }
  }
}

// ─── inline SVG charts (no charting dependency) ────────────────────────────

/** A tiny area sparkline; null points render as gaps. */
function Sparkline({
  values,
  width = 220,
  height = 48,
  min,
  max,
}: {
  values: (number | null)[]
  width?: number
  height?: number
  min?: number
  max?: number
}) {
  const nums = values.filter((v): v is number => v != null)
  if (nums.length === 0) return null
  const lo = min ?? Math.min(...nums)
  const hi = max ?? Math.max(...nums)
  const span = hi - lo || 1
  const stepX = width / Math.max(values.length - 1, 1)
  const pts = values
    .map((v, i) => (v == null ? null : `${i * stepX},${height - 4 - ((v - lo) / span) * (height - 8)}`))
    .filter(Boolean)
  if (pts.length === 0) return null
  const firstIdx = values.findIndex((v) => v != null)
  const lastIdx = values.length - 1 - [...values].reverse().findIndex((v) => v != null)
  const line = pts.join(' ')
  const area = `${firstIdx * stepX},${height} ${line} ${lastIdx * stepX},${height}`
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label="trend sparkline"
      className="overflow-visible"
    >
      <polygon points={area} fill="var(--accent-soft)" />
      <polyline
        points={line}
        fill="none"
        stroke="var(--accent)"
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}

/** Horizontal bars for 0..10 metrics. */
function ScaleBars({ label, value, max }: { label: string; value: number | null; max: number }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, (value / max) * 100))
  return (
    <div className="flex items-center gap-2">
      <span className="w-14 shrink-0 text-right text-xs text-muted">{label}</span>
      <div
        className="h-2 flex-1 overflow-hidden rounded-full bg-hover"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value ?? 0}
        aria-label={label}
      >
        <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-8 text-xs tabular-nums text-secondary">
        {value == null ? '—' : `${value}/${max}`}
      </span>
    </div>
  )
}

// ─── sub-components ────────────────────────────────────────────────────────

function TrendCard({
  title,
  trend,
  values,
  range,
}: {
  title: string
  trend: TrendBlock | undefined
  values: (number | null)[]
  range: { min?: number; max?: number }
}) {
  const dir = trend ? directionWord(trend.direction) : directionWord('insufficient')
  return (
    <div className="rounded-lg border border-line bg-elevated p-4">
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-primary">{title}</h3>
        <span className={`text-xs font-medium ${dir.color}`}>
          {trend?.direction === 'insufficient' ? dir.text : dir.text}
        </span>
      </div>
      <div className="mt-2">
        {trend && trend.n >= 3 ? (
          <Sparkline values={values} min={range.min} max={range.max} />
        ) : (
          <div className="flex h-12 items-center text-xs text-muted">
            {trend ? `n=${trend.n} — ${trend.description}` : 'no data'}
          </div>
        )}
      </div>
      <p className="mt-2 text-xs text-secondary">
        {trend ? trend.description : 'No data recorded for this window.'}
      </p>
      {trend && trend.n >= 3 && (
        <p className="mt-1 text-xs tabular-nums text-muted">
          mean {trend.mean?.toFixed(2)} · first {trend.first} · last {trend.last}
        </p>
      )}
    </div>
  )
}

function CorrelationCard({ block }: { block: CorrelationBlock }) {
  const insufficient = block.status !== 'ok'
  return (
    <div className="rounded-lg border border-line bg-elevated p-4">
      <h3 className="text-sm font-semibold text-primary">Sleep vs. mood</h3>
      <p className="mt-1 text-xs text-secondary">{block.description}</p>
      {insufficient ? (
        <p className="mt-2 text-xs italic text-muted">
          Insufficient data — {block.n} of 7+ days with both sleep and mood recorded.
        </p>
      ) : (
        <div className="mt-2">
          <div
            className="relative h-2 rounded-full bg-hover"
            role="img"
            aria-label={`correlation r=${block.r}`}
          >
            {/* zero mark at center */}
            <div className="absolute left-1/2 top-0 h-full w-px bg-border" />
            <div
              className="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent"
              style={{ left: `${50 + ((block.r ?? 0) * 50).toFixed(1)}%` }}
            />
          </div>
          <div className="mt-1 flex justify-between text-[10px] text-muted">
            <span>−1</span>
            <span className="tabular-nums">r = {block.r?.toFixed(2)} · n = {block.n}</span>
            <span>+1</span>
          </div>
        </div>
      )}
    </div>
  )
}

function SleepCard({ sleep, present }: { sleep: SleepConsistency; present: boolean }) {
  return (
    <div className="rounded-lg border border-line bg-elevated p-4">
      <h3 className="text-sm font-semibold text-primary">Sleep</h3>
      {!present ? (
        <p className="mt-1 text-xs text-muted">No sleep data — nothing to show.</p>
      ) : (
        <>
          <p className="mt-1 text-xs text-secondary">{sleep.description}</p>
          <div className="mt-2 flex gap-4 text-xs tabular-nums text-secondary">
            <span>
              avg {fmtDuration(sleep.avg_minutes)}
            </span>
            <span>
              ±{sleep.stdev_minutes == null ? '—' : `${Math.round(sleep.stdev_minutes)} min`}
            </span>
            <span className="text-muted">{sleep.n} days recorded</span>
          </div>
        </>
      )}
    </div>
  )
}

// ─── main view ─────────────────────────────────────────────────────────────

export default function WellnessView() {
  const [status, setStatus] = useState<WellnessStatus | null>(null)
  const [entries, setEntries] = useState<WellnessEntry[]>([])
  const [trends, setTrends] = useState<WellnessTrends | null>(null)
  const [windowDays, setWindowDays] = useState<7 | 30 | 90>(30)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  // check-in form state
  const [mood, setMood] = useState<number | ''>('')
  const [energy, setEnergy] = useState<number | ''>('')
  const [stress, setStress] = useState<number | ''>('')
  const [sleepMinutes, setSleepMinutes] = useState<number | ''>('')
  const [journal, setJournal] = useState('')
  const [obsKey, setObsKey] = useState('')
  const [obsValue, setObsValue] = useState('')
  const [observations, setObservations] = useState<Record<string, unknown>>({})
  const [obsAdded, setObsAdded] = useState(false)

  // export + delete dialogs
  const [exportOpen, setExportOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleteTyped, setDeleteTyped] = useState('')
  const [notice, setNotice] = useState<string | null>(null)

  const todayEntry = useMemo(() => {
    const today = new Date().toISOString().slice(0, 10)
    return entries.find((e) => e.at.slice(0, 10) === today) ?? null
  }, [entries])

  const refresh = useCallback(async (days: number) => {
    try {
      const [st, es, tr] = await Promise.all([
        wellnessApi.getStatus(),
        wellnessApi.listEntries(),
        wellnessApi.getTrends(days),
      ])
      setStatus(st)
      setEntries(es)
      setTrends(tr)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load wellness data')
    }
  }, [])

  useEffect(() => {
    void refresh(windowDays)
  }, [refresh, windowDays])

  const flash = (msg: string) => {
    setNotice(msg)
    window.setTimeout(() => setNotice(null), 4000)
  }

  const saveCheckin = async (id: string | null) => {
    setBusy('save')
    try {
      const payload: CheckInPayload = {
        mood: mood === '' ? null : Number(mood),
        energy: energy === '' ? null : Number(energy),
        stress: stress === '' ? null : Number(stress),
        sleep_minutes: sleepMinutes === '' ? null : Number(sleepMinutes),
        journal_entry: journal || null,
        observations: Object.keys({ ...observations, ...pickObs() }).length
          ? { ...observations, ...pickObs() }
          : null,
      }
      if (id) {
        await wellnessApi.updateEntry(id, payload)
      } else {
        await wellnessApi.checkin(payload)
      }
      setJournal('')
      setObservations({})
      setObsAdded(false)
      flash('Check-in saved.')
      await refresh(windowDays)
    } catch (e) {
      flash(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setBusy(null)
    }
  }

  const pickObs = (): Record<string, unknown> => {
    if (!obsAdded || !obsKey.trim()) return {}
    return { [obsKey.trim()]: obsValue }
  }

  const addObservation = () => {
    if (!obsKey.trim()) return
    setObservations((prev) => ({ ...prev, [obsKey.trim()]: obsValue }))
    setObsKey('')
    setObsValue('')
    setObsAdded(true)
  }

  const toggleEnabled = async (enabled: boolean) => {
    setBusy('status')
    try {
      await wellnessApi.patchStatus({ enabled })
      flash(enabled ? 'Tracking enabled.' : 'Tracking disabled — data is kept.')
      await refresh(windowDays)
    } catch (e) {
      flash(e instanceof Error ? e.message : 'Status update failed')
    } finally {
      setBusy(null)
    }
  }

  const pauseUntil = async () => {
    const when = window.prompt(
      'Pause tracking until (ISO date, e.g. 2026-11-01):',
      new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10)
    )
    if (!when) return
    setBusy('status')
    try {
      await wellnessApi.patchStatus({ paused_until: `${when}T23:59:59.000Z` })
      flash(`Paused until ${when}.`)
      await refresh(windowDays)
    } catch (e) {
      flash(e instanceof Error ? e.message : 'Pause failed')
    } finally {
      setBusy(null)
    }
  }

  const clearPause = async () => {
    setBusy('status')
    try {
      await wellnessApi.patchStatus({ clear_pause: true })
      flash('Pause cleared.')
      await refresh(windowDays)
    } catch (e) {
      flash(e instanceof Error ? e.message : 'Unpause failed')
    } finally {
      setBusy(null)
    }
  }

  const runExport = async () => {
    setBusy('export')
    try {
      const receipt = await wellnessApi.export('csv')
      await wellnessApi.downloadCsv()
      flash(
        `Export of ${receipt.entries} entries downloaded — recorded in the audit log (${receipt.audit.code}).`
      )
      setExportOpen(false)
    } catch (e) {
      flash(e instanceof Error ? e.message : 'Export failed')
      setExportOpen(false)
    } finally {
      setBusy(null)
    }
  }

  const runDeleteAll = async () => {
    if (deleteTyped !== 'DELETE') return
    setBusy('delete')
    try {
      const n = await wellnessApi.deleteAll()
      flash(`Deleted ${n} entries — all wellness data removed.`)
      setDeleteOpen(false)
      setDeleteTyped('')
      await refresh(windowDays)
    } catch (e) {
      flash(e instanceof Error ? e.message : 'Delete failed')
    } finally {
      setBusy(null)
    }
  }

  const trendValues = (metric: string): (number | null)[] => {
    if (!trends) return []
    const fields: Record<string, (e: WellnessEntry) => number | null> = {
      mood: (e) => e.mood,
      energy: (e) => e.energy,
      stress: (e) => e.stress,
      sleep_minutes: (e) => e.sleep_minutes,
    }
    const get = fields[metric] ?? (() => null)
    return [...entries]
      .sort((a, b) => (a.at > b.at ? 1 : -1))
      .map((e) => get(e))
      .slice(0, windowDays)
  }

  const disabled = status?.enabled === false
  const paused = !!status?.paused_until && status.paused_until > new Date().toISOString()

  return (
    <div className="space-y-6">
      {/* header + privacy statement */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-primary">Wellness Journal</h1>
          <p className="mt-1 max-w-2xl text-sm text-secondary">
            Wellness data stays in NerveCenter and is never sent to other services or models.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted">
          {status?.durable === false && (
            <span className="rounded bg-hover px-2 py-1">demo mode — not persisted</span>
          )}
          <span
            className={`rounded px-2 py-1 font-medium ${
              disabled ? 'bg-danger/20 text-danger' : paused ? 'bg-warning/20 text-warning' : 'bg-success/20 text-success'
            }`}
          >
            {disabled ? 'tracking off' : paused ? `paused until ${fmtDate(status?.paused_until)}` : 'tracking on'}
            {status ? ` · ${status.entry_count} entries` : ''}
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

      <div className="grid gap-6 lg:grid-cols-2">
        {/* ── check-in card ─────────────────────────────────────────────── */}
        <section aria-label="Check in" className="rounded-lg border border-line bg-elevated p-4">
          <div className="flex items-baseline justify-between">
            <h2 className="text-base font-semibold text-primary">
              Today's check-in {todayEntry ? '· saved' : ''}
            </h2>
            <span className="text-xs text-muted">one entry per day — saving again updates today</span>
          </div>

          <div className="mt-3 space-y-3">
            {/* mood 1..5 */}
            <div>
              <label className="text-xs font-medium text-secondary">Mood (1–5)</label>
              <div className="mt-1 flex gap-1.5">
                {[1, 2, 3, 4, 5].map((v) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => setMood(mood === v ? '' : v)}
                    aria-pressed={mood === v}
                    className={`flex-1 rounded border px-2 py-1.5 text-xs transition ${
                      mood === v
                        ? 'border-accent bg-accent-soft text-accent-contrast'
                        : 'border-line bg-transparent text-muted hover:bg-hover'
                    }`}
                  >
                    {v}
                    <span className="block text-[10px] text-muted">{MOOD_LABELS[v]}</span>
                  </button>
                ))}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs font-medium text-secondary">
                Energy (1–10)
                <input
                  type="number"
                  min={1}
                  max={10}
                  value={energy}
                  onChange={(e) => setEnergy(e.target.value === '' ? '' : Number(e.target.value))}
                  className="mt-1 w-full rounded border border-line bg-transparent px-2 py-1.5 text-sm"
                />
              </label>
              <label className="text-xs font-medium text-secondary">
                Stress (1–10)
                <input
                  type="number"
                  min={1}
                  max={10}
                  value={stress}
                  onChange={(e) => setStress(e.target.value === '' ? '' : Number(e.target.value))}
                  className="mt-1 w-full rounded border border-line bg-transparent px-2 py-1.5 text-sm"
                />
              </label>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs font-medium text-secondary">
                Sleep (minutes, optional)
                <input
                  type="number"
                  min={0}
                  value={sleepMinutes}
                  onChange={(e) =>
                    setSleepMinutes(e.target.value === '' ? '' : Number(e.target.value))
                  }
                  className="mt-1 w-full rounded border border-line bg-transparent px-2 py-1.5 text-sm"
                />
              </label>
              <label className="text-xs font-medium text-secondary">
                Custom observation key
                <div className="mt-1 flex gap-1">
                  <input
                    value={obsKey}
                    onChange={(e) => setObsKey(e.target.value)}
                    placeholder="e.g. caffeine"
                    className="w-1/2 rounded border border-line bg-transparent px-2 py-1.5 text-sm"
                  />
                  <input
                    value={obsValue}
                    onChange={(e) => setObsValue(e.target.value)}
                    placeholder="value"
                    className="w-1/2 rounded border border-line bg-transparent px-2 py-1.5 text-sm"
                  />
                  <button
                    type="button"
                    onClick={addObservation}
                    disabled={!obsKey.trim()}
                    className="rounded border border-line px-2 py-1.5 text-xs text-secondary hover:bg-hover disabled:opacity-40"
                  >
                    Add
                  </button>
                </div>
              </label>
            </div>

            {Object.keys(observations).length > 0 && (
              <ul className="space-y-1">
                {Object.entries(observations).map(([k, v]) => (
                  <li key={k} className="flex items-center justify-between rounded bg-hover px-2 py-1 text-xs">
                    <span className="text-secondary">{k}</span>
                    <span className="flex items-center gap-2 text-muted">
                      {String(v)}
                      <button
                        type="button"
                        onClick={() =>
                          setObservations((prev) => {
                            const next = { ...prev }
                            delete next[k]
                            return next
                          })
                        }
                        aria-label={`remove observation ${k}`}
                        className="text-danger hover:underline"
                      >
                        remove
                      </button>
                    </span>
                  </li>
                ))}
              </ul>
            )}

            <label className="block text-xs font-medium text-secondary">
              Journal entry (optional)
              <textarea
                value={journal}
                onChange={(e) => setJournal(e.target.value)}
                rows={3}
                placeholder="How is today going?"
                className="mt-1 w-full resize-y rounded border border-line bg-transparent px-2 py-1.5 text-sm"
              />
            </label>

            <button
              type="button"
              onClick={() => saveCheckin(todayEntry?.id ?? null)}
              disabled={busy !== null}
              className="w-full rounded bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
            >
              {todayEntry ? "Update today's check-in" : 'Save check-in'}
            </button>
          </div>
        </section>

        {/* ── controls: enable / pause / export / delete ─────────────────── */}
        <section aria-label="Privacy controls" className="rounded-lg border border-line bg-elevated p-4">
          <h2 className="text-base font-semibold text-primary">Your controls</h2>
          <p className="mt-1 text-xs text-muted">
            Disable stops new collection (data is kept). Delete removes everything.
          </p>

          <div className="mt-3 space-y-2">
            <div className="flex items-center justify-between rounded border border-line p-3">
              <div>
                <p className="text-sm text-secondary">Tracking enabled</p>
                <p className="text-xs text-muted">
                  {disabled ? 'off — no new check-ins recorded' : 'on'}
                </p>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => toggleEnabled(false)}
                  disabled={busy !== null || disabled}
                  className="rounded border border-line px-3 py-1.5 text-xs text-secondary hover:bg-hover disabled:opacity-40"
                >
                  Disable
                </button>
                <button
                  type="button"
                  onClick={() => toggleEnabled(true)}
                  disabled={busy !== null || !disabled}
                  className="rounded border border-line px-3 py-1.5 text-xs text-secondary hover:bg-hover disabled:opacity-40"
                >
                  Enable
                </button>
              </div>
            </div>

            <div className="flex items-center justify-between rounded border border-line p-3">
              <div>
                <p className="text-sm text-secondary">Pause</p>
                <p className="text-xs text-muted">
                  {paused
                    ? `paused until ${fmtDate(status?.paused_until)}`
                    : 'no active pause'}
                </p>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={pauseUntil}
                  disabled={busy !== null}
                  className="rounded border border-line px-3 py-1.5 text-xs text-secondary hover:bg-hover disabled:opacity-40"
                >
                  Pause until…
                </button>
                <button
                  type="button"
                  onClick={clearPause}
                  disabled={busy !== null || !paused}
                  className="rounded border border-line px-3 py-1.5 text-xs text-secondary hover:bg-hover disabled:opacity-40"
                >
                  Resume
                </button>
              </div>
            </div>

            <div className="flex items-center justify-between rounded border border-line p-3">
              <div>
                <p className="text-sm text-secondary">Clinician export</p>
                <p className="text-xs text-muted">
                  Downloads all entries as CSV and records the export in the audit log.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setExportOpen(true)}
                disabled={busy !== null}
                className="rounded border border-line px-3 py-1.5 text-xs text-secondary hover:bg-hover disabled:opacity-40"
              >
                Export…
              </button>
            </div>

            <div className="flex items-center justify-between rounded border border-danger/40 p-3">
              <div>
                <p className="text-sm text-secondary">Delete all wellness data</p>
                <p className="text-xs text-muted">
                  Permanently removes every entry. This cannot be undone.
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setDeleteOpen(true)
                  setDeleteTyped('')
                }}
                disabled={busy !== null}
                className="rounded border border-danger/50 px-3 py-1.5 text-xs text-danger hover:bg-danger/10 disabled:opacity-40"
              >
                Delete all…
              </button>
            </div>
          </div>
        </section>
      </div>

      {/* ── trends ──────────────────────────────────────────────────────── */}
      <section aria-label="Trends" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold text-primary">Trends</h2>
          <div className="flex gap-1" role="tablist" aria-label="Trend window">
            {([7, 30, 90] as const).map((d) => (
              <button
                key={d}
                type="button"
                role="tab"
                aria-selected={windowDays === d}
                onClick={() => setWindowDays(d)}
                className={`rounded px-3 py-1 text-xs font-medium ${
                  windowDays === d
                    ? 'bg-accent-soft text-accent-contrast'
                    : 'text-muted hover:bg-hover'
                }`}
              >
                {d}d
              </button>
            ))}
          </div>
        </div>
        <p className="text-xs text-muted">
          Statistics only — these describe your recorded numbers; nothing here is a diagnosis.
        </p>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <TrendCard
            title="Mood"
            trend={trends?.trends.mood}
            values={trendValues('mood')}
            range={{ min: 1, max: 5 }}
          />
          <TrendCard
            title="Energy"
            trend={trends?.trends.energy}
            values={trendValues('energy')}
            range={{ min: 1, max: 10 }}
          />
          <TrendCard
            title="Stress"
            trend={trends?.trends.stress}
            values={trendValues('stress')}
            range={{ min: 1, max: 10 }}
          />
          <TrendCard
            title="Sleep duration"
            trend={trends?.trends.sleep_minutes}
            values={trendValues('sleep_minutes')}
            range={{}}
          />
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <SleepCard sleep={trends?.sleep ?? {
            n: 0,
            avg_minutes: null,
            stdev_minutes: null,
            regularity: 'unknown',
            description: 'no sleep data',
          }} present={!!trends?.sleep_data_present} />
          <CorrelationCard block={trends?.correlation ?? {
            metric: 'sleep_vs_mood',
            n: 0,
            r: null,
            status: 'insufficient',
            description: 'insufficient data',
          }} />
        </div>
      </section>

      {/* ── history ─────────────────────────────────────────────────────── */}
      <section aria-label="History">
        <h2 className="text-lg font-semibold text-primary">History</h2>
        {entries.length === 0 ? (
          <div className="mt-3 rounded-lg border border-line bg-elevated p-6 text-center text-sm text-muted">
            No check-ins yet. Save today's above to start your journal.
          </div>
        ) : (
          <div className="mt-3 overflow-x-auto rounded-lg border border-line bg-elevated">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-3 py-2 font-medium">Date</th>
                  <th className="px-3 py-2 font-medium">Mood</th>
                  <th className="px-3 py-2 font-medium">Sleep</th>
                  <th className="px-3 py-2 font-medium">Energy</th>
                  <th className="px-3 py-2 font-medium">Stress</th>
                  <th className="px-3 py-2 font-medium">Notes</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.id} className="border-b border-line last:border-0 hover:bg-hover/40">
                    <td className="whitespace-nowrap px-3 py-2 text-secondary">
                      {fmtDate(e.at)}
                    </td>
                    <td className="px-3 py-2">
                      <ScaleBars label="" value={e.mood} max={5} />
                    </td>
                    <td className="px-3 py-2 tabular-nums text-secondary">
                      {fmtDuration(e.sleep_minutes)}
                    </td>
                    <td className="px-3 py-2 tabular-nums text-secondary">{e.energy ?? '—'}</td>
                    <td className="px-3 py-2 tabular-nums text-secondary">{e.stress ?? '—'}</td>
                    <td className="max-w-[220px] truncate px-3 py-2 text-secondary" title={e.journal_entry ?? ''}>
                      {e.journal_entry || (e.observations && Object.keys(e.observations).length > 0
                        ? Object.entries(e.observations).map(([k, v]) => `${k}: ${v}`).join(', ')
                        : '—')}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        onClick={async () => {
                          if (!window.confirm('Delete this check-in?')) return
                          await wellnessApi.deleteEntry(e.id)
                          flash('Entry deleted.')
                          await refresh(windowDays)
                        }}
                        className="text-xs text-danger hover:underline"
                      >
                        delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ── export confirmation dialog ──────────────────────────────────── */}
      {exportOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Confirm clinician export"
          onClick={() => setExportOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-lg border border-line bg-elevated p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold text-primary">Export your wellness data</h3>
            <p className="mt-2 text-sm text-secondary">
              This downloads every entry as a CSV file to share with a clinician, and the
              export is <span className="text-primary">recorded in the audit log</span>.
            </p>
            <p className="mt-2 text-xs text-muted">
              Wellness data stays in NerveCenter; the export goes only to the file on this
              device.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setExportOpen(false)}
                className="rounded border border-line px-3 py-2 text-sm text-secondary hover:bg-hover"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={runExport}
                disabled={busy !== null}
                className="rounded bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
              >
                Download & record
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── delete-all double confirmation ──────────────────────────────── */}
      {deleteOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Confirm deleting all wellness data"
          onClick={() => setDeleteOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-lg border border-danger/40 bg-elevated p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold text-danger">
              Delete ALL wellness data?
            </h3>
            <p className="mt-2 text-sm text-secondary">
              Every check-in, journal entry, and observation will be permanently deleted.
              This cannot be undone.
            </p>
            <label className="mt-4 block text-xs font-medium text-secondary">
              Type <span className="font-mono text-danger">DELETE</span> to confirm
              <input
                value={deleteTyped}
                onChange={(e) => setDeleteTyped(e.target.value)}
                className="mt-1 w-full rounded border border-danger/50 bg-transparent px-2 py-1.5 font-mono text-sm"
                autoFocus
              />
            </label>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteOpen(false)}
                className="rounded border border-line px-3 py-2 text-sm text-secondary hover:bg-hover"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={runDeleteAll}
                disabled={deleteTyped !== 'DELETE' || busy !== null}
                className="rounded bg-danger px-3 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-40"
              >
                Delete everything
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
