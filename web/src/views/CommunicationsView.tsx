import { useCallback, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  communicationsApi,
  type CommunicationRow,
  type PersonRequestGroup,
  type SearchMeta,
} from '../api/communications'

/**
 * CommunicationsView — cross-channel communications search (R110 / §50,
 * P1-107 gap B).
 *
 * Lets a signed-in user search their ingested communications via
 * `GET /api/communications/search` and read the matched records. The results
 * are metadata-only (sender / subject / timestamp / channel / person link +
 * `content_ref`) — the search backend never returns raw bodies or tokens
 * (§7), and this view never requests them.
 *
 * States: an initial/unsearched state (distinct from the "no matches" empty
 * state), a loading state, an error banner (OV-1 contract), the flat matched
 * communications, and the optional 'who asked me' request groups when the
 * query carries request intent.
 *
 * Auth: the client attaches the session bearer via `apiFetch`; a 401 flows
 * through the shared unauthorized handler (sign-in redirect), like the other
 * authorized views.
 */

/** Render an API failure as `<status> — <detail>` (OV-1 contract). */
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
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

/** The subject for a matched record: classification.subject, else subject. */
function subjectOf(row: CommunicationRow): string | null {
  const cls = row.classification as { subject?: unknown } | null
  if (cls && typeof cls.subject === 'string' && cls.subject) return cls.subject
  if (typeof row.subject === 'string' && row.subject) return row.subject
  return null
}

const CHANNELS: { value: string; label: string }[] = [
  { value: '', label: 'All channels' },
  { value: 'email', label: 'Email' },
  { value: 'imessage', label: 'iMessage' },
  { value: 'sms', label: 'SMS' },
  { value: 'rcs', label: 'RCS' },
  { value: 'voicemail', label: 'Voicemail' },
]

function ResultRow({ row }: { row: CommunicationRow }) {
  const subject = subjectOf(row)
  const who = row.sender || (row.recipients[0] ?? 'Unknown')
  return (
    <li
      data-testid={`comm-result-${row.communication_id}`}
      className="rounded-lg border border-line bg-elevated px-4 py-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="min-w-0 truncate text-sm font-medium text-primary">{who}</span>
        <span className="shrink-0 rounded bg-hover px-1.5 py-0.5 text-[11px] font-medium text-secondary">
          {row.channel}
        </span>
      </div>
      {subject && <p className="mt-0.5 truncate text-sm text-secondary">{subject}</p>}
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted">
        <span>{formatWhen(row.timestamp)}</span>
        {row.person_id ? (
          <Link
            to={`/people`}
            className="font-medium text-blue-600 hover:underline dark:text-blue-400"
            title={`Linked person ${row.person_id}`}
          >
            {row.person_id}
          </Link>
        ) : (
          <span className="italic">unlinked</span>
        )}
      </div>
    </li>
  )
}

function RequestGroups({ groups }: { groups: PersonRequestGroup[] }) {
  if (groups.length === 0) return null
  return (
    <section className="space-y-2">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">
        Who asked you
      </h2>
      <ul className="space-y-2">
        {groups.map((g) => (
          <li
            key={g.person_id}
            data-testid={`comm-request-group-${g.person_id}`}
            className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-700 dark:bg-amber-900/20"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium text-amber-800 dark:text-amber-200">
                {g.display_name ?? g.person_id}
              </span>
              <span className="text-xs text-amber-700 dark:text-amber-300">
                {g.communication_ids.length} request
                {g.communication_ids.length === 1 ? '' : 's'}
              </span>
            </div>
            <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">
              {formatWhen(g.first_request_at)} → {formatWhen(g.last_request_at)}
            </p>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function CommunicationsView() {
  const [query, setQuery] = useState('')
  const [channel, setChannel] = useState('')
  const [withinWeek, setWithinWeek] = useState(true)
  const [rows, setRows] = useState<CommunicationRow[]>([])
  const [searchMeta, setSearchMeta] = useState<SearchMeta | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // `searched` distinguishes the initial/unsearched state from "no matches".
  const [searched, setSearched] = useState(false)

  const runSearch = useCallback(
    async (e: FormEvent) => {
      e.preventDefault()
      const q = query.trim()
      if (!q) return
      setLoading(true)
      setError(null)
      try {
        const res = await communicationsApi.search({
          q,
          channel: channel || undefined,
          withinWeek,
        })
        setRows(res.data)
        setSearchMeta(res.meta.search)
        setSearched(true)
      } catch (err) {
        setError(formatApiError(err))
        setRows([])
        setSearchMeta(null)
        setSearched(true)
      } finally {
        setLoading(false)
      }
    },
    [query, channel, withinWeek],
  )

  const matchedKeywords = searchMeta?.matched_keywords ?? []
  const requestGroups = searchMeta?.request_groups ?? []

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-primary">Search</h1>
        <p className="text-sm text-muted">
          {searched && !loading
            ? `${rows.length} match${rows.length === 1 ? '' : 'es'}`
            : 'Search your ingested communications'}
        </p>
      </div>

      <form onSubmit={runSearch} className="space-y-3">
        <div className="flex flex-wrap gap-2">
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search messages, emails, voicemails…"
            aria-label="Search communications"
            data-testid="comm-search-input"
            className="min-w-0 flex-1 rounded-lg border border-gray-300 bg-elevated px-3 py-2 text-sm text-primary placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600"
          />
          <select
            value={channel}
            onChange={(e) => setChannel(e.target.value)}
            aria-label="Filter by channel"
            data-testid="comm-channel-select"
            className="rounded-lg border border-gray-300 bg-elevated px-3 py-2 text-sm text-primary focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600"
          >
            {CHANNELS.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={loading || query.trim() === ''}
            data-testid="comm-search-submit"
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? 'Searching…' : 'Search'}
          </button>
        </div>
        <label className="flex items-center gap-2 text-sm text-secondary">
          <input
            type="checkbox"
            checked={withinWeek}
            onChange={(e) => setWithinWeek(e.target.checked)}
            aria-label="Limit to the last week"
            data-testid="comm-within-week"
            className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
          />
          Only the last week
        </label>
      </form>

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-900/30 dark:text-red-300"
        >
          {error}
        </div>
      )}

      {matchedKeywords.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <span>matched:</span>
          {matchedKeywords.map((k) => (
            <span
              key={k}
              className="rounded bg-hover px-1.5 py-0.5 font-medium text-secondary"
            >
              {k}
            </span>
          ))}
        </div>
      )}

      {!error && <RequestGroups groups={requestGroups} />}

      {loading ? (
        <p className="text-sm text-muted" data-testid="comm-loading">
          Searching…
        </p>
      ) : !searched ? (
        <div
          className="rounded-lg border border-dashed border-line bg-elevated px-4 py-6 text-center text-sm text-muted"
          data-testid="comm-initial-state"
        >
          Type a query and search to find your ingested communications.
        </div>
      ) : error ? null : rows.length === 0 ? (
        <div
          className="rounded-lg border border-line bg-elevated px-4 py-6 text-center text-sm text-muted"
          data-testid="comm-empty-state"
        >
          No matches — nothing in your ingested communications matched that query.
        </div>
      ) : (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">
            Matches
          </h2>
          <ul className="space-y-2" data-testid="comm-results">
            {rows.map((row) => (
              <ResultRow key={row.communication_id} row={row} />
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
