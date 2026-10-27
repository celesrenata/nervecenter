import { useState, useEffect, useCallback, useMemo, type ReactNode } from 'react'
import {
  peopleApi,
  type Person,
  type PersonDetail,
  type PersonProfile,
  type UnansweredEntry,
  type SocialPlan,
  type PersonNote,
  type PersonBoundary,
  type Birthday,
  type ReminderItem,
} from '../api/people'

/**
 * PeopleView — the user-controlled People & Relationships workspace
 * (Personal-Ops Recovery spec §11, workstream J).
 *
 * Three tabs, all backed by the frozen §11 routes:
 *
 *   People    — canonical person list (GET /api/people), one factual record
 *               per person (GET /api/people/{id}), the durable editor
 *               (PATCH /api/people/{id}: VIP, notes, boundaries, birthdays —
 *               full-replace semantics), and follow-up reminders
 *               (POST /api/people/{id}/reminders).
 *   Unanswered — the *factual* surfacing: newest inbound older than the
 *               threshold with no outbound reply after it
 *               (GET /api/relationships/unanswered).
 *   Social    — explicit social plans only: user-marked events or verbatim
 *               attendee-handle matches (GET /api/relationships/social).
 *               No fuzzy matching anywhere in this view.
 *
 * API failures surface as a `role="alert"` banner (OV-1 contract); the
 * durable routes degrade honestly (503) when the durable plane is absent.
 */

function formatApiError(err: unknown): string {
  const e = err as { status?: number; detail?: string; message?: string } | null
  if (e && typeof e.status === 'number' && typeof e.detail === 'string') {
    return `${e.status} — ${e.detail}`
  }
  if (e && typeof e.message === 'string' && e.message) return e.message
  return 'Request failed'
}

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return 'Not recorded'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

type Tab = 'people' | 'unanswered' | 'social'

const TABS: { id: Tab; label: string }[] = [
  { id: 'people', label: 'People' },
  { id: 'unanswered', label: 'Unanswered' },
  { id: 'social', label: 'Social plans' },
]

// ─── People tab: list + detail ──────────────────────────────────────────────

function PersonListRow({
  person,
  active,
  onSelect,
}: {
  person: Person
  active: boolean
  onSelect: (id: string) => void
}) {
  const identities = person.identities
    .map((i) => i.value)
    .filter(Boolean)
    .slice(0, 2)
    .join(' · ')
  return (
    <button
      type="button"
      onClick={() => onSelect(person.person_id)}
      aria-current={active ? 'true' : undefined}
      className={`w-full rounded-lg border px-3 py-2.5 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 ${
        active
          ? 'border-blue-400 bg-blue-50 dark:border-blue-500 dark:bg-blue-900/30'
          : 'border-line bg-elevated hover:bg-hover'
      }`}
    >
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-primary">
          {person.display_name ?? 'Unknown person'}
        </span>
        {person.is_vip && (
          <span
            className="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
            title="VIP"
          >
            VIP
          </span>
        )}
      </div>
      {identities && <p className="mt-0.5 truncate text-xs text-muted">{identities}</p>}
    </button>
  )
}

function Section({
  title,
  count,
  children,
}: {
  title: string
  count?: number
  children: ReactNode
}) {
  return (
    <section className="rounded-lg border border-line bg-elevated">
      <h3 className="flex items-center gap-2 border-b border-line px-4 py-2.5 text-sm font-semibold text-primary">
        {title}
        {count !== undefined && (
          <span className="rounded-full bg-hover px-2 py-0.5 text-xs font-medium text-secondary">
            {count}
          </span>
        )}
      </h3>
      <div className="px-4 py-3 text-sm text-secondary">{children}</div>
    </section>
  )
}

function EmptyLine({ children }: { children: ReactNode }) {
  return <p className="text-muted italic">{children}</p>
}

// ─── People tab: the durable editor ─────────────────────────────────────────

function NoteEditor({
  notes,
  onChange,
  saving,
}: {
  notes: PersonNote[]
  onChange: (next: PersonNote[]) => void
  saving: boolean
}) {
  const [draft, setDraft] = useState(notes.map((n) => n.text).join('\n'))
  useEffect(() => {
    setDraft(notes.map((n) => n.text).join('\n'))
  }, [notes])
  return (
    <div className="space-y-2">
      <label htmlFor="notes-editor" className="text-xs font-medium text-muted">
        One note per line — saving replaces the stored notes.
      </label>
      <textarea
        id="notes-editor"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        disabled={saving}
        rows={Math.max(3, notes.length + 1)}
        className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-elevated px-3 py-2 text-sm text-primary placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-blue-500"
        placeholder="met at the conference"
      />
      <button
        type="button"
        disabled={saving}
        onClick={() =>
          onChange(
            draft
              .split('\n')
              .map((line) => line.trim())
              .filter(Boolean)
              .map((text) => ({ at: null, text })),
          )
        }
        className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {saving ? 'Saving…' : 'Save notes'}
      </button>
    </div>
  )
}

function BoundaryEditor({
  boundaries,
  onChange,
  saving,
}: {
  boundaries: PersonBoundary[]
  onChange: (next: PersonBoundary[]) => void
  saving: boolean
}) {
  const update = (i: number, patch: Partial<PersonBoundary>) =>
    onChange(boundaries.map((b, idx) => (idx === i ? { ...b, ...patch } : b)))
  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <button
          type="button"
          disabled={saving}
          onClick={() => onChange([...boundaries, { scope: 'general', rule: '', active: true }])}
          className="rounded-md border border-line px-3 py-1.5 text-xs font-medium text-secondary hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50"
        >
          Add boundary
        </button>
      </div>
      {boundaries.length === 0 && <EmptyLine>No boundaries recorded.</EmptyLine>}
      {boundaries.map((b, i) => (
        <div key={i} className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            aria-label="Boundary scope"
            value={b.scope}
            onChange={(e) => update(i, { scope: e.target.value })}
            className="w-28 rounded-md border border-gray-300 dark:border-gray-600 bg-elevated px-2 py-1.5 text-xs text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <input
            type="text"
            aria-label="Boundary rule"
            value={b.rule}
            onChange={(e) => update(i, { rule: e.target.value })}
            placeholder="weekends off"
            className="min-w-0 flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-elevated px-2 py-1.5 text-xs text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <label className="flex items-center gap-1.5 text-xs text-secondary">
            <input
              type="checkbox"
              checked={b.active}
              onChange={(e) => update(i, { active: e.target.checked })}
              className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
            />
            active
          </label>
          <button
            type="button"
            aria-label="Remove boundary"
            disabled={saving}
            onClick={() => onChange(boundaries.filter((_, idx) => idx !== i))}
            className="text-xs text-muted hover:text-red-600 dark:hover:text-red-400 disabled:opacity-50"
          >
            remove
          </button>
        </div>
      ))}
      {boundaries.length > 0 && (
        <button
          type="button"
          disabled={saving}
          onClick={() => onChange(boundaries.filter((b) => b.rule.trim() !== ''))}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save boundaries'}
        </button>
      )}
    </div>
  )
}

function BirthdayEditor({
  birthdays,
  onChange,
  saving,
}: {
  birthdays: Birthday[]
  onChange: (next: Birthday[]) => void
  saving: boolean
}) {
  const update = (i: number, patch: Partial<Birthday>) =>
    onChange(birthdays.map((b, idx) => (idx === i ? { ...b, ...patch } : b)))
  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <button
          type="button"
          disabled={saving}
          onClick={() =>
            onChange([...birthdays, { date: '', name: '', occurs_on: null, in_days: null }])
          }
          className="rounded-md border border-line px-3 py-1.5 text-xs font-medium text-secondary hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50"
        >
          Add birthday
        </button>
      </div>
      {birthdays.length === 0 && <EmptyLine>No birthdays recorded.</EmptyLine>}
      {birthdays.map((b, i) => (
        <div key={i} className="flex flex-wrap items-center gap-2">
          <input
            type="date"
            aria-label="Birthday date"
            value={b.date}
            onChange={(e) => update(i, { date: e.target.value })}
            className="rounded-md border border-gray-300 dark:border-gray-600 bg-elevated px-2 py-1.5 text-xs text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <input
            type="text"
            aria-label="Birthday name"
            value={b.name ?? ''}
            onChange={(e) => update(i, { name: e.target.value })}
            placeholder="name (optional)"
            className="min-w-0 flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-elevated px-2 py-1.5 text-xs text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          {b.occurs_on && (
            <span className="text-xs text-muted">
              next: {b.occurs_on}
              {typeof b.in_days === 'number' ? ` (${b.in_days} days)` : ''}
            </span>
          )}
          <button
            type="button"
            aria-label="Remove birthday"
            disabled={saving}
            onClick={() => onChange(birthdays.filter((_, idx) => idx !== i))}
            className="text-xs text-muted hover:text-red-600 dark:hover:text-red-400 disabled:opacity-50"
          >
            remove
          </button>
        </div>
      ))}
      {birthdays.length > 0 && (
        <button
          type="button"
          disabled={saving}
          onClick={() => onChange(birthdays.filter((b) => b.date !== ''))}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save birthdays'}
        </button>
      )}
    </div>
  )
}

function ReminderForm({
  personId,
  onCreated,
}: {
  personId: string
  onCreated: (r: ReminderItem) => void
}) {
  const [text, setText] = useState('')
  const [dueAt, setDueAt] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (!text.trim()) return
    setSaving(true)
    setError(null)
    try {
      const res = await peopleApi.createReminder(personId, {
        text: text.trim(),
        due_at: dueAt ? new Date(dueAt).toISOString() : null,
      })
      onCreated(res.item)
      setText('')
      setDueAt('')
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="space-y-2">
      {error && (
        <div role="alert" className="rounded-md border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/30 px-3 py-2 text-xs text-red-700 dark:text-red-300">
          {error}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <input
          type="text"
          aria-label="Reminder text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="check in about the contract"
          className="min-w-0 flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-elevated px-2 py-1.5 text-xs text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <input
          type="date"
          aria-label="Reminder due date"
          value={dueAt}
          onChange={(e) => setDueAt(e.target.value)}
          className="rounded-md border border-gray-300 dark:border-gray-600 bg-elevated px-2 py-1.5 text-xs text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button
          type="button"
          disabled={saving || text.trim() === ''}
          onClick={submit}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? 'Adding…' : 'Add reminder'}
        </button>
      </div>
    </div>
  )
}

function PersonDetail({
  personId,
  onSaved,
}: {
  personId: string
  onSaved: (p: Person) => void
}) {
  const [detail, setDetail] = useState<PersonDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [notesDraft, setNotesDraft] = useState<PersonNote[] | null>(null)
  const [boundariesDraft, setBoundariesDraft] = useState<PersonBoundary[] | null>(null)
  const [birthdaysDraft, setBirthdaysDraft] = useState<Birthday[] | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const d = await peopleApi.getPerson(personId)
      setDetail(d)
      setNotesDraft(null)
      setBoundariesDraft(null)
      setBirthdaysDraft(null)
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setLoading(false)
    }
  }, [personId])

  useEffect(() => {
    void load()
  }, [load])

  const profile = detail?.relationships
  const notes = notesDraft ?? profile?.notes ?? []
  const boundaries = boundariesDraft ?? profile?.boundaries ?? []
  const birthdays = birthdaysDraft ?? profile?.birthdays ?? []

  const patch = useCallback(
    async (body: Record<string, unknown>) => {
      setSaving(true)
      setError(null)
      try {
        const res = await peopleApi.patchPerson(personId, body)
        // Full-replace editors: refresh the whole detail so the profile
        // (occurrence-augmented birthdays, normalized notes) stays authoritative.
        const refreshed = await peopleApi.getPerson(personId)
        setDetail(refreshed)
        setNotesDraft(null)
        setBoundariesDraft(null)
        setBirthdaysDraft(null)
        onSaved(refreshed.item)
        void res
      } catch (err) {
        setError(formatApiError(err))
      } finally {
        setSaving(false)
      }
    },
    [personId, onSaved],
  )

  if (loading) {
    return (
      <div className="rounded-lg border border-line bg-elevated px-4 py-3 text-sm text-muted">
        Loading record…
      </div>
    )
  }
  if (error) {
    return (
      <div
        role="alert"
        className="rounded-lg border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/30 px-4 py-3 text-sm text-red-700 dark:text-red-300"
      >
        {error}
        <button type="button" onClick={load} className="mt-2 block text-xs font-medium underline">
          Retry
        </button>
      </div>
    )
  }
  if (!detail) return null
  const p: Person = detail.item
  const prof: PersonProfile | undefined = profile

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-xl font-semibold text-primary">
            {p.display_name ?? 'Unknown person'}
          </h2>
          <p className="text-xs text-muted">{p.person_id}</p>
        </div>
        <button
          type="button"
          disabled={saving}
          onClick={() => void patch({ is_vip: !p.is_vip })}
          className={`rounded-md border px-3 py-1.5 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50 ${
            p.is_vip
              ? 'border-amber-300 bg-amber-100 text-amber-800 dark:border-amber-600 dark:bg-amber-900/40 dark:text-amber-300'
              : 'border-line text-secondary hover:bg-hover'
          }`}
        >
          {p.is_vip ? '★ VIP — remove' : '☆ Mark VIP'}
        </button>
      </div>

      {prof === undefined ? (
        <div className="rounded-lg border border-dashed border-line bg-elevated px-4 py-3 text-xs text-muted">
          The durable relationships plane is not configured on this server
          (503) — the person record is shown without its relationship details.
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          <Section title="Notes" count={notes.length}>
            <NoteEditor notes={notes} onChange={setNotesDraft} saving={saving} />
            {notes.length > 0 && (
              <div className="mt-2 flex justify-end">
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => void patch({ notes })}
                  className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saving ? 'Saving…' : 'Save notes'}
                </button>
              </div>
            )}
          </Section>

          <Section title="Boundaries" count={boundaries.length}>
            <BoundaryEditor
              boundaries={boundaries}
              onChange={setBoundariesDraft}
              saving={saving}
            />
            {boundaries.length > 0 && (
              <div className="mt-2 flex justify-end">
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => void patch({ boundaries })}
                  className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saving ? 'Saving…' : 'Save boundaries'}
                </button>
              </div>
            )}
          </Section>

          <Section title="Birthdays" count={birthdays.length}>
            <BirthdayEditor
              birthdays={birthdays}
              onChange={setBirthdaysDraft}
              saving={saving}
            />
            {birthdays.length > 0 && (
              <div className="mt-2 flex justify-end">
                <button
                  type="button"
                  disabled={saving}
                  onClick={() =>
                    void patch({
                      birthdays: birthdays.map((b) => ({
                        date: b.date,
                        name: b.name || undefined,
                      })),
                    })
                  }
                  className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saving ? 'Saving…' : 'Save birthdays'}
                </button>
              </div>
            )}
          </Section>

          <Section title="Identities" count={prof.identities.length}>
            {prof.identities.length === 0 && <EmptyLine>No linked identities.</EmptyLine>}
            <ul className="space-y-1">
              {prof.identities.map((i) => (
                <li key={`${i.channel}:${i.handle}`} className="flex items-center gap-2">
                  <span className="rounded bg-hover px-1.5 py-0.5 text-[11px] font-medium text-secondary">
                    {i.channel}
                  </span>
                  <span className="min-w-0 truncate text-secondary">{i.handle}</span>
                  <span className={`ml-auto shrink-0 text-[11px] ${i.resolved ? 'text-muted' : 'text-amber-600 dark:text-amber-400'}`}>
                    {i.resolved ? 'resolved' : 'unresolved'}
                  </span>
                </li>
              ))}
            </ul>
          </Section>

          <Section title="Unanswered" count={prof.unanswered.length}>
            {prof.unanswered.length === 0 && (
              <EmptyLine>No conversations waiting on you.</EmptyLine>
            )}
            <ul className="space-y-2">
              {prof.unanswered.map((u) => (
                <li key={`${u.channel}:${u.last_inbound_at}`} className="space-y-0.5">
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-hover px-1.5 py-0.5 text-[11px] font-medium text-secondary">
                      {u.channel}
                    </span>
                    <span className="font-medium text-primary">{u.age_days} days</span>
                  </div>
                  {u.subject && <p className="truncate text-xs">{u.subject}</p>}
                  <p className="text-[11px] text-muted">last inbound {formatWhen(u.last_inbound_at)}</p>
                </li>
              ))}
            </ul>
          </Section>

          <Section title="Promises made" count={prof.promises_made.length}>
            {prof.promises_made.length === 0 && (
              <EmptyLine>No outgoing-sourced promises on record.</EmptyLine>
            )}
            <ul className="space-y-2">
              {prof.promises_made.map((pr) => (
                <li key={pr.id} className="space-y-0.5">
                  <p className="text-secondary">{pr.text}</p>
                  <p className="text-[11px] text-muted">
                    <span className="rounded bg-hover px-1.5 py-0.5 text-[11px] font-medium text-secondary">
                      {pr.state}
                    </span>{' '}
                    {pr.due_at ? `due ${formatWhen(pr.due_at)}` : 'no due date'}
                  </p>
                </li>
              ))}
            </ul>
          </Section>

          <Section title="Follow-ups" count={prof.follow_ups.length}>
            <ReminderForm
              personId={personId}
              onCreated={() => void load()}
            />
            <ul className="mt-3 space-y-2">
              {prof.follow_ups.map((f) => (
                <li key={f.id} className="space-y-0.5">
                  <p className={`text-secondary ${f.is_due ? 'font-medium' : ''}`}>
                    {f.text}
                    {f.is_due && (
                      <span className="ml-2 rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-700 dark:bg-red-900/40 dark:text-red-300">
                        due
                      </span>
                    )}
                  </p>
                  <p className="text-[11px] text-muted">
                    {f.due_at ? `due ${formatWhen(f.due_at)}` : 'no due date'}
                  </p>
                </li>
              ))}
              {prof.follow_ups.length === 0 && <EmptyLine>No open follow-ups.</EmptyLine>}
            </ul>
          </Section>

          <Section title="Social plans" count={prof.social_plans.length}>
            {prof.social_plans.length === 0 && <EmptyLine>No social plans on the calendar.</EmptyLine>}
            <ul className="space-y-2">
              {prof.social_plans.map((s) => (
                <li key={s.event_id} className="space-y-0.5">
                  <p className="text-secondary">{s.title}</p>
                  <p className="text-[11px] text-muted">
                    {formatWhen(s.start)}
                    {s.location ? ` · ${s.location}` : ''}
                    {s.marked_social ? ' · marked social' : ' · attendee match'}
                  </p>
                </li>
              ))}
            </ul>
          </Section>
        </div>
      )}
    </div>
  )
}

// ─── People tab ─────────────────────────────────────────────────────────────

function PeopleTab() {
  const [people, setPeople] = useState<Person[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await peopleApi.listPeople()
      setPeople(res.data)
      setSelectedId((cur) => cur ?? res.data[0]?.person_id ?? null)
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return people
    return people.filter(
      (p) =>
        (p.display_name ?? '').toLowerCase().includes(q) ||
        p.identities.some((i) => i.value.toLowerCase().includes(q)),
    )
  }, [people, query])

  const selected = selectedId ?? filtered[0]?.person_id ?? null

  return (
    <div className="space-y-4">
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search by name or identity…"
        aria-label="Search people"
        className="w-full max-w-md rounded-lg border border-gray-300 dark:border-gray-600 bg-elevated px-3 py-2 text-sm text-primary placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/30 px-4 py-3 text-sm text-red-700 dark:text-red-300"
        >
          {error}
          <button type="button" onClick={() => void load()} className="mt-2 block text-xs font-medium underline">
            Retry
          </button>
        </div>
      )}
      {loading ? (
        <p className="text-sm text-muted">Loading people…</p>
      ) : error ? null : filtered.length === 0 ? (
        <p className="text-sm text-muted italic">No people match.</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(220px,320px)_1fr]">
          <div className="space-y-2" role="listbox" aria-label="People">
            {filtered.map((person) => (
              <PersonListRow
                key={person.person_id}
                person={person}
                active={person.person_id === selected}
                onSelect={setSelectedId}
              />
            ))}
          </div>
          {selected ? (
            <PersonDetail
              personId={selected}
              onSaved={(updated) =>
                setPeople((prev) =>
                  prev.map((p) => (p.person_id === updated.person_id ? updated : p)),
                )
              }
            />
          ) : (
            <div className="rounded-lg border border-dashed border-line bg-elevated px-4 py-3 text-sm text-muted">
              Select a person to see their record.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Unanswered tab ─────────────────────────────────────────────────────────

function UnansweredTab() {
  const [entries, setEntries] = useState<UnansweredEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await peopleApi.unanswered()
      setEntries(res.data)
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="space-y-3">
      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/30 px-4 py-3 text-sm text-red-700 dark:text-red-300"
        >
          {error}
        </div>
      )}
      {loading && !error ? (
        <p className="text-sm text-muted">Loading unanswered conversations…</p>
      ) : entries.length === 0 && !error ? (
        <p className="text-sm text-muted italic">
          Nothing unanswered — every inbound conversation has had a reply after it.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line bg-elevated">
          <table className="w-full min-w-[520px] text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs uppercase tracking-wide text-muted">
                <th className="px-4 py-2.5 font-medium">Person</th>
                <th className="px-4 py-2.5 font-medium">Channel</th>
                <th className="px-4 py-2.5 font-medium">Waiting</th>
                <th className="px-4 py-2.5 font-medium">Subject</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr
                  key={`${e.person_id ?? 'x'}:${e.channel}:${e.last_inbound_at}`}
                  className="border-b border-line last:border-b-0"
                >
                  <td className="px-4 py-2.5 font-medium text-primary">
                    {e.display_name ?? e.person_id ?? 'Unknown'}
                  </td>
                  <td className="px-4 py-2.5 text-secondary">
                    <span className="rounded bg-hover px-1.5 py-0.5 text-[11px] font-medium">
                      {e.channel}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-secondary">
                    {e.age_days} days <span className="text-[11px] text-muted">({formatWhen(e.last_inbound_at)})</span>
                  </td>
                  <td className="max-w-0 truncate px-4 py-2.5 text-secondary">
                    {e.subject ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ─── Social tab ─────────────────────────────────────────────────────────────

function SocialTab() {
  const [plans, setPlans] = useState<SocialPlan[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await peopleApi.socialPlans({ days: 14 })
      setPlans(res.data)
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="space-y-3">
      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/30 px-4 py-3 text-sm text-red-700 dark:text-red-300"
        >
          {error}
        </div>
      )}
      {loading && !error ? (
        <p className="text-sm text-muted">Loading social plans…</p>
      ) : plans.length === 0 && !error ? (
        <p className="text-sm text-muted italic">
          No social plans in the next two weeks. Mark an event social or add your
          handle to its attendees to surface it here.
        </p>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2">
          {plans.map((s) => (
            <li key={s.event_id} className="rounded-lg border border-line bg-elevated px-4 py-3">
              <p className="text-sm font-medium text-primary">{s.title}</p>
              <p className="mt-1 text-xs text-muted">{formatWhen(s.start)}</p>
              {s.location && <p className="text-xs text-muted">{s.location}</p>}
              {s.participants.length > 0 && (
                <p className="mt-2 text-[11px] text-muted">
                  {s.participants.join(' · ')}
                </p>
              )}
              <p className="mt-2 text-[11px] font-medium text-secondary">
                {s.marked_social ? 'marked social' : 'attendee match'}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ─── View ───────────────────────────────────────────────────────────────────

export function PeopleView() {
  const [tab, setTab] = useState<Tab>('people')
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-primary">People & Relationships</h1>
        <p className="mt-1 text-sm text-muted">
          Your people, factually: notes, boundaries, birthdays, what is waiting on
          you, and what you have promised.
        </p>
      </div>

      <div role="tablist" aria-label="Relationships sections" className="flex flex-wrap gap-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 ${
              tab === t.id
                ? 'border-blue-400 bg-blue-50 text-blue-700 dark:border-blue-500 dark:bg-blue-900/30 dark:text-blue-300'
                : 'border-line bg-elevated text-secondary hover:bg-hover'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'people' && <PeopleTab />}
      {tab === 'unanswered' && <UnansweredTab />}
      {tab === 'social' && <SocialTab />}
    </div>
  )
}
