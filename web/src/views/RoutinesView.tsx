import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  routinesApi,
  ROUTINE_KINDS,
  type CheckInPayload,
  type MissedNote,
  type RoutinesToday,
  type Routine,
  type RoutineKind,
  type RoutineSlot,
} from '../api/routines'

/**
 * RoutinesView — personal routines (Personal-Ops Recovery §9).
 *
 * **No streaks, no guilt** — a missed window is ONE gentle note, and a
 * skipped check-in is still "handled". Sleep regularity is explicit
 * records only: missing data keeps regularity at "unknown" and never
 * asserts asleep/awake. Degrades to an honest "demo mode — not
 * persisted" state when no durable registry is configured.
 */

// ─── helpers ────────────────────────────────────────────────────────────────

const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
// Backend weekdays are ISO: 0=Mon … 6=Sun.

const KIND_LABELS: Record<RoutineKind, string> = {
  wake: 'Wake',
  workday_prep: 'Workday prep',
  bedtime: 'Bedtime',
  morning_briefing: 'Morning briefing',
  appointment_prep: 'Appointment prep',
  medication: 'Medication',
  check_in: 'Check-in',
}

function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  return d.toLocaleString([], sameDay ? { hour: 'numeric', minute: '2-digit' } : { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

function fmtDuration(minutes: number | null): string {
  if (minutes == null || !Number.isFinite(minutes)) return '—'
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

function describeSchedule(r: Routine): string {
  const s = r.schedule || {}
  const times = s.times?.length ? s.times.join(', ') : 'no times set'
  const days =
    s.weekdays && s.weekdays.length
      ? s.weekdays.slice().sort((a, b) => a - b).map((w) => WEEKDAY_LABELS[w] ?? String(w)).join(' · ')
      : 'every day'
  const tz = s.timezone ? ` · ${s.timezone}` : ''
  return `${times} — ${days}${tz}`
}

function kindBadge(kind: string): string {
  return KIND_LABELS[kind as RoutineKind] ?? kind
}

// ─── slot row with Check-in / Complete ──────────────────────────────────────

function SlotRow({
  slot,
  state,
  busy,
  onAction,
}: {
  slot: RoutineSlot
  state: 'due' | 'upcoming'
  busy: boolean
  onAction: (slot: RoutineSlot, action: 'checkin' | 'complete') => void
}) {
  return (
    <li className="bg-elevated px-4 py-3 rounded-lg shadow-sm flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm text-primary truncate">{slot.title}</div>
        <div className="mt-0.5 text-xs text-muted tabular-nums">
          {slot.time} {slot.timezone}
          {state === 'due' && (
            <span className="ml-2 rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning">
              due now
            </span>
          )}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => onAction(slot, 'checkin')}
          className="rounded border border-line px-3 py-1.5 text-xs font-medium text-secondary hover:bg-hover disabled:opacity-40"
        >
          Check-in
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onAction(slot, 'complete')}
          className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-accent-contrast disabled:opacity-40"
        >
          Complete
        </button>
      </div>
    </li>
  )
}

// ─── routine list row ───────────────────────────────────────────────────────

function RoutineRow({
  routine,
  busy,
  onToggle,
}: {
  routine: Routine
  busy: boolean
  onToggle: (r: Routine) => void
}) {
  return (
    <tr className="border-b border-line/60 last:border-b-0">
      <td className="px-3 py-2 align-top">
        <span className="rounded bg-hover px-1.5 py-0.5 text-[10px] font-medium text-secondary">
          {kindBadge(routine.kind)}
        </span>
      </td>
      <td className="px-3 py-2 text-primary">{routine.title}</td>
      <td className="px-3 py-2 text-secondary">{describeSchedule(routine)}</td>
      <td className="px-3 py-2 text-right">
        <button
          type="button"
          disabled={busy}
          onClick={() => onToggle(routine)}
          className={`rounded px-3 py-1.5 text-xs font-medium disabled:opacity-40 ${
            routine.enabled
              ? 'bg-success/20 text-success'
              : 'border border-line text-secondary hover:bg-hover'
          }`}
          aria-pressed={routine.enabled}
        >
          {routine.enabled ? 'Enabled' : 'Paused'}
        </button>
      </td>
    </tr>
  )
}

// ─── add-routine form ───────────────────────────────────────────────────────

function AddRoutineForm({
  busy,
  onCreated,
  onError,
}: {
  busy: boolean
  onCreated: (title: string) => void
  onError: (msg: string) => void
}) {
  const [kind, setKind] = useState<RoutineKind>('check_in')
  const [title, setTitle] = useState('')
  const [times, setTimes] = useState('')
  const [weekdays, setWeekdays] = useState<number[]>([])
  const [timezone, setTimezone] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)

  const toggleDay = (d: number) =>
    setWeekdays((prev) => (prev.includes(d) ? prev.filter((x) => x !== d) : [...prev, d]))

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setLocalError(null)
    const timeList = times
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)
    if (!title.trim()) {
      setLocalError('Give the routine a name.')
      return
    }
    if (timeList.length && timeList.some((t) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(t))) {
      setLocalError('Times must be HH:MM (24-hour), comma-separated.')
      return
    }
    try {
      await routinesApi.create({
        kind,
        title: title.trim(),
        schedule:
          timeList.length || timezone.trim()
            ? { times: timeList, weekdays, timezone: timezone.trim() || undefined }
            : undefined,
        enabled: true,
      })
      setTitle('')
      setTimes('')
      setTimezone('')
      onCreated(title.trim())
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Could not create the routine.')
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3" aria-label="Add a routine">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="text-xs text-secondary">
          Kind
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as RoutineKind)}
            className="mt-1 w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-primary"
          >
            {ROUTINE_KINDS.map((k) => (
              <option key={k} value={k}>
                {KIND_LABELS[k]}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-secondary">
          Name
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Morning stretch"
            className="mt-1 w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-primary"
          />
        </label>
        <label className="text-xs text-secondary">
          Times (HH:MM, comma-separated)
          <input
            value={times}
            onChange={(e) => setTimes(e.target.value)}
            placeholder="07:00, 13:00"
            className="mt-1 w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-primary"
          />
        </label>
        <label className="text-xs text-secondary">
          Timezone (IANA)
          <input
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            placeholder="America/Los_Angeles"
            list="iana-timezones"
            className="mt-1 w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-primary"
          />
          <datalist id="iana-timezones">
            <option value="America/Los_Angeles" />
            <option value="America/New_York" />
            <option value="Europe/Berlin" />
            <option value="Europe/London" />
            <option value="Asia/Tokyo" />
            <option value="UTC" />
          </datalist>
        </label>
      </div>
      <div className="text-xs text-secondary">
        Days <span className="text-muted">(empty = every day)</span>
        <div className="mt-1 flex flex-wrap gap-1">
          {WEEKDAY_LABELS.map((label, i) => (
            <button
              key={label}
              type="button"
              onClick={() => toggleDay(i)}
              aria-pressed={weekdays.includes(i)}
              className={`rounded px-2 py-1 text-xs font-medium ${
                weekdays.includes(i)
                  ? 'bg-accent text-accent-contrast'
                  : 'border border-line text-secondary hover:bg-hover'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {localError && <p role="alert" className="text-xs text-danger">{localError}</p>}
      <button
        type="submit"
        disabled={busy}
        className="rounded bg-accent px-3 py-1.5 text-xs font-medium text-accent-contrast disabled:opacity-40"
      >
        Add routine
      </button>
    </form>
  )
}

// ─── sleep section ──────────────────────────────────────────────────────────

function SleepSection({ today, durable }: { today: RoutinesToday | null; durable: boolean | null }) {
  const [bedAt, setBedAt] = useState('')
  const [wakeAt, setWakeAt] = useState('')
  const [saved, setSaved] = useState<string[]>([])
  const [sleepError, setSleepError] = useState<string | null>(null)

  const sleep = today?.sleep
  const regularity = sleep?.regularity ?? 'unknown'
  const regularityCopy =
    regularity === 'stable'
      ? 'stable pattern'
      : regularity === 'variable'
        ? 'variable pattern'
        : 'unknown — not enough records to say'

  // Explicit records only (source: manual). This entry is kept locally —
  // the frozen §9 surface carries no durable sleep-write route, so it is
  // labelled as such and never presented as persisted.
  const saveLocal = (e: FormEvent) => {
    e.preventDefault()
    setSleepError(null)
    if (!bedAt) {
      setSleepError('When did you get to bed?')
      return
    }
    if (new Date(bedAt).getTime() === Number.NaN) {
      setSleepError('That bedtime does not parse — pick a valid date-time.')
      return
    }
    if (wakeAt && new Date(wakeAt).getTime() === Number.NaN) {
      setSleepError('That wake time does not parse — pick a valid date-time.')
      return
    }
    const entry = `${new Date(bedAt).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}${
      wakeAt ? ` → up ${new Date(wakeAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : ' (still asleep)'
    }`
    setSaved((prev) => [entry, ...prev].slice(0, 7))
    setBedAt('')
    setWakeAt('')
  }

  return (
    <section aria-labelledby="routines-sleep" className="space-y-3">
      <h2 id="routines-sleep" className="text-lg font-semibold text-primary">Sleep</h2>
      <div className="bg-elevated p-4 rounded-lg shadow-sm space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-primary font-medium">{regularityCopy}</span>
          <span className="text-xs text-muted tabular-nums">
            {sleep ? `${sleep.n} record${sleep.n === 1 ? '' : 's'}` : 'no data'}
            {sleep?.avg_minutes != null ? ` · avg ${fmtDuration(sleep.avg_minutes)}` : ''}
          </span>
        </div>
        {sleep?.description && <p className="text-xs text-secondary">{sleep.description}</p>}
        {!durable && (
          <p className="text-xs text-muted">
            No durable store — regularity stays unknown. Missing sleep data never proves
            asleep or awake.
          </p>
        )}
        {saved.length > 0 && (
          <ul className="space-y-1 text-xs text-secondary" aria-label="Records for this session">
            {saved.map((s, i) => (
              <li key={`${s}-${i}`}>• {s}</li>
            ))}
          </ul>
        )}
        <form onSubmit={saveLocal} className="space-y-2" aria-label="Record sleep (session only)">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <span>Record a night</span>
            <span className="rounded bg-hover px-1.5 py-0.5 text-[10px] text-secondary">
              this session only — not persisted to NerveCenter
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <label className="text-xs text-secondary">
              Went to bed
              <input
                type="datetime-local"
                value={bedAt}
                onChange={(e) => setBedAt(e.target.value)}
                className="mt-1 w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-primary"
              />
            </label>
            <label className="text-xs text-secondary">
              Woke up <span className="text-muted">(optional)</span>
              <input
                type="datetime-local"
                value={wakeAt}
                onChange={(e) => setWakeAt(e.target.value)}
                className="mt-1 w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-primary"
              />
            </label>
            <div className="flex items-end">
              <button
                type="submit"
                className="w-full rounded border border-line px-3 py-1.5 text-xs font-medium text-secondary hover:bg-hover"
              >
                Save record
              </button>
            </div>
          </div>
          {sleepError && <p role="alert" className="text-xs text-danger">{sleepError}</p>}
        </form>
      </div>
    </section>
  )
}

// ─── main view ─────────────────────────────────────────────────────────────

export default function RoutinesView() {
  const [routines, setRoutines] = useState<Routine[]>([])
  const [today, setToday] = useState<RoutinesToday | null>(null)
  const [durable, setDurable] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const [list, todayView] = await Promise.all([routinesApi.list(), routinesApi.today()])
      setRoutines(list.data)
      setToday(todayView)
      setDurable(list.durable)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load routines.')
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const actOnSlot = async (slot: RoutineSlot, action: 'checkin' | 'complete') => {
    setBusy(true)
    setNotice(null)
    try {
      const payload: CheckInPayload = { completed: true }
      if (action === 'checkin') {
        await routinesApi.checkin(slot.routine_id, payload)
        setNotice(`Recorded: ${slot.title}.`)
      } else {
        await routinesApi.complete(slot.routine_id)
        setNotice(`Completed: ${slot.title}.`)
      }
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not handle “${slot.title}”.`)
    } finally {
      setBusy(false)
    }
  }

  const toggleRoutine = async (r: Routine) => {
    setBusy(true)
    try {
      const res = await routinesApi.patch(r.id, { enabled: !r.enabled })
      setRoutines((prev) => prev.map((x) => (x.id === res.routine.id ? res.routine : x)))
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not update “${r.title}”.`)
    } finally {
      setBusy(false)
    }
  }

  const missedNotes: MissedNote[] = today?.missed_notes ?? []
  const dueNow = today?.due_now ?? []
  const upcoming = today?.upcoming ?? []
  const completedToday = today?.completed_today ?? []

  return (
    <div className="space-y-6">
      {/* header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-primary">Routines</h1>
          <p className="mt-1 max-w-2xl text-sm text-secondary">
            Your day's shape — wake, prep, bedtime, the rest. Nothing here
            counts streaks or guilt; a missed slot is a quiet one-line note.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted">
          {durable === false && (
            <span className="rounded bg-hover px-2 py-1">demo mode — not persisted</span>
          )}
          <span className="rounded bg-hover px-2 py-1 tabular-nums">
            {routines.length} routine{routines.length === 1 ? '' : 's'}
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

      {/* Today panel */}
      <section aria-labelledby="routines-today" className="space-y-3">
        <h2 id="routines-today" className="text-lg font-semibold text-primary">Today</h2>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="bg-elevated p-4 rounded-lg shadow-sm space-y-3">
            <h3 className="text-sm font-medium text-primary">Due now</h3>
            {dueNow.length === 0 ? (
              <p className="text-xs text-secondary">Nothing due right now.</p>
            ) : (
              <ul className="space-y-2">
                {dueNow.map((slot) => (
                  <SlotRow key={slot.id} slot={slot} state="due" busy={busy} onAction={actOnSlot} />
                ))}
              </ul>
            )}
            <h3 className="text-sm font-medium text-primary pt-2">Upcoming (48h)</h3>
            {upcoming.length === 0 ? (
              <p className="text-xs text-secondary">No upcoming slots.</p>
            ) : (
              <ul className="space-y-2">
                {upcoming.slice(0, 6).map((slot) => (
                  <SlotRow key={slot.id} slot={slot} state="upcoming" busy={busy} onAction={actOnSlot} />
                ))}
                {upcoming.length > 6 && (
                  <div className="text-[10px] text-muted">+{upcoming.length - 6} more</div>
                )}
              </ul>
            )}
            {completedToday.length > 0 && (
              <>
                <h3 className="text-sm font-medium text-primary pt-2">Handled today</h3>
                <ul className="space-y-1 text-xs text-secondary">
                  {completedToday.map((ci) => (
                    <li key={ci.id} className="tabular-nums">
                      ✓ {fmtWhen(ci.at)} — {routines.find((r) => r.id === ci.routine_id)?.title ?? 'routine'}
                      {!ci.completed && ' (skipped — still handled)'}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>

          {/* missed notes — one gentle line each, never a streak */}
          <div className="bg-elevated p-4 rounded-lg shadow-sm space-y-2">
            <h3 className="text-sm font-medium text-primary">Quiet notes</h3>
            {missedNotes.length === 0 ? (
              <p className="text-xs text-secondary">No notes — nothing was missed.</p>
            ) : (
              <ul className="space-y-2">
                {missedNotes.map((n) => (
                  <li key={n.id} className="text-sm text-secondary border-l-2 border-line pl-3">
                    {n.note}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </section>

      {/* routines list */}
      <section aria-labelledby="routines-list" className="space-y-3">
        <h2 id="routines-list" className="text-lg font-semibold text-primary">Routines</h2>
        <div className="bg-elevated rounded-lg shadow-sm overflow-x-auto">
          <table className="w-full min-w-[480px] text-left text-sm">
            <thead className="text-xs text-muted">
              <tr className="border-b border-line/60">
                <th className="px-3 py-2 font-medium">Kind</th>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Schedule</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {routines.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-6 text-center text-sm text-secondary">
                    No routines yet — add one below.
                  </td>
                </tr>
              ) : (
                routines.map((r) => (
                  <RoutineRow key={r.id} routine={r} busy={busy} onToggle={toggleRoutine} />
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* add routine */}
      <section aria-labelledby="add-routine" className="space-y-3">
        <h2 id="add-routine" className="text-lg font-semibold text-primary">Add a routine</h2>
        <div className="bg-elevated p-4 rounded-lg shadow-sm">
          <AddRoutineForm
            busy={busy}
            onCreated={(title) => setNotice(`Added “${title}”.`)}
            onError={(msg) => setError(msg)}
          />
        </div>
      </section>

      {/* sleep */}
      <SleepSection today={today} durable={durable} />
    </div>
  )
}
