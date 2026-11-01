import { useCallback, useEffect, useRef, useState } from 'react'
import { logsApi } from '../api/logs'
import type { LogEntry, LogLevel } from '../api/logs'

/**
 * Log level → badge styling. Every level carries visible text plus an
 * `aria-label`; the dot is decorative and never the sole signal (D-1).
 * Uses the project's semantic palette so light/dark both stay legible.
 */
const LEVEL_STYLE: Record<
  LogLevel,
  { text: string; bg: string; ring: string; dot: string }
> = {
  debug: {
    text: 'text-gray-700 dark:text-gray-300',
    bg: 'bg-gray-100 dark:bg-gray-800/60',
    ring: 'border-gray-300 dark:border-gray-600',
    dot: 'bg-gray-400',
  },
  info: {
    text: 'text-green-700 dark:text-green-300',
    bg: 'bg-green-100 dark:bg-green-900/40',
    ring: 'border-green-300 dark:border-green-700',
    dot: 'bg-green-500',
  },
  warning: {
    text: 'text-yellow-700 dark:text-yellow-300',
    bg: 'bg-yellow-100 dark:bg-yellow-900/40',
    ring: 'border-yellow-300 dark:border-yellow-700',
    dot: 'bg-yellow-500',
  },
  error: {
    text: 'text-red-700 dark:text-red-300',
    bg: 'bg-red-100 dark:bg-red-900/40',
    ring: 'border-red-300 dark:border-red-700',
    dot: 'bg-red-500',
  },
  critical: {
    text: 'text-red-800 dark:text-red-200',
    bg: 'bg-red-200 dark:bg-red-900/70',
    ring: 'border-red-400 dark:border-red-600',
    dot: 'bg-red-600',
  },
}

const LEVELS: (LogLevel | '')[] = ['', 'debug', 'info', 'warning', 'error', 'critical']

const DEFAULT_LIMIT = 200
const REFRESH_MS = 3000

/**
 * Render an API failure as `<status> — <detail>`. Duck-types the shared
 * `ApiError { status, detail }` contract so it works whether or not the
 * client has adopted `ApiError` yet (mirrors SystemsView).
 */
function formatApiError(err: unknown): string {
  if (err && typeof err === 'object') {
    const e = err as { status?: number; detail?: string; message?: string }
    if (typeof e.status === 'number' && typeof e.detail === 'string') {
      return `${e.status} — ${e.detail}`
    }
    if (typeof e.message === 'string' && e.message) return e.message
  }
  return 'Request failed'
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString()
}

/**
 * Live in-service logs view (§49). Streams the most recent rows from the
 * backend ring buffer, honoring a level filter and a cap on rows. Refresh is
 * optional (auto-refresh toggle) so the view stays honest: when the buffer is
 * empty it says so rather than inventing rows.
 */
const LogsView = () => {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [level, setLevel] = useState<LogLevel | ''>('')
  const [limit, setLimit] = useState(DEFAULT_LIMIT)
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [secretsScrubbed, setSecretsScrubbed] = useState(true)
  const requestSeq = useRef(0)

  const fetchLogs = useCallback(async () => {
    const seq = ++requestSeq.current
    setRefreshing(true)
    try {
      const res = await logsApi.getLogs({ limit, level })
      // Drop stale responses if a newer request has since started.
      if (seq !== requestSeq.current) return
      setEntries(res.data ?? [])
      setSecretsScrubbed(res.secrets_scrubbed)
      setLastUpdated(new Date())
      setError(null)
    } catch (err) {
      if (seq !== requestSeq.current) return
      setError(formatApiError(err))
    } finally {
      if (seq === requestSeq.current) {
        setRefreshing(false)
        setLoading(false)
      }
    }
  }, [limit, level])

  // Initial + manual fetch whenever the query (level/limit) changes.
  useEffect(() => {
    void fetchLogs()
  }, [fetchLogs])

  // Optional auto-refresh: poll every REFRESH_MS while enabled.
  useEffect(() => {
    if (!autoRefresh) return
    const id = window.setInterval(() => {
      void fetchLogs()
    }, REFRESH_MS)
    return () => window.clearInterval(id)
  }, [autoRefresh, fetchLogs])

  const countByLevel = entries.reduce<Partial<Record<LogLevel, number>>>((acc, e) => {
    acc[e.level] = (acc[e.level] ?? 0) + 1
    return acc
  }, {})

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-primary">Logs</h1>
          <p className="text-sm text-muted mt-1">
            Live in-service log stream ·{' '}
            <span aria-live="polite">
              {entries.length} row{entries.length === 1 ? '' : 's'} in buffer
            </span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          {secretsScrubbed && (
            <span
              role="note"
              aria-label="Secrets scrubbed"
              className="text-xs text-muted inline-flex items-center gap-1"
            >
              <span
                className="w-2 h-2 rounded-full bg-green-500"
                aria-hidden="true"
              />
              secrets scrubbed
            </span>
          )}
          <button
            type="button"
            onClick={() => void fetchLogs()}
            disabled={loading || refreshing}
            className="px-3 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-elevated text-sm font-medium text-primary hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {/* Filter + limit toolbar */}
      <div className="bg-elevated rounded-lg shadow-sm p-4 flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="logs-level" className="text-xs font-medium text-muted">
            Minimum level
          </label>
          <select
            id="logs-level"
            value={level}
            onChange={(e) => setLevel(e.target.value as LogLevel | '')}
            className="px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-sm text-primary min-w-[8rem]"
          >
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l === '' ? 'All levels' : l}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="logs-limit" className="text-xs font-medium text-muted">
            Max rows
          </label>
          <select
            id="logs-limit"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-sm text-primary min-w-[6rem]"
          >
            {[50, 100, 200, 500].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>

        <label className="flex items-center gap-2 mt-5 text-sm text-primary cursor-pointer">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
            className="w-4 h-4 rounded border-gray-300 dark:border-gray-600"
          />
          Auto-refresh every {REFRESH_MS / 1000}s
        </label>

        {lastUpdated && (
          <span className="text-xs text-muted ml-auto mt-5">
            Updated {lastUpdated.toLocaleTimeString()}
          </span>
        )}
      </div>

      {/* Level summary chips */}
      {entries.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {(Object.keys(countByLevel) as LogLevel[])
            .filter((l) => (countByLevel[l] ?? 0) > 0)
            .map((l) => {
              const s = LEVEL_STYLE[l]
              return (
                <span
                  key={l}
                  className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-medium ${s.bg} ${s.ring} ${s.text}`}
                >
                  <span className={`w-2 h-2 rounded-full ${s.dot}`} aria-hidden="true" />
                  {l}
                  <span className="tabular-nums">{countByLevel[l]}</span>
                </span>
              )
            })}
        </div>
      )}

      {loading ? (
        <div className="text-center py-12" role="status">
          Loading logs…
        </div>
      ) : error ? (
        <div
          role="alert"
          className="text-red-700 dark:text-red-300 bg-red-100 dark:bg-red-900/40 border border-red-300 dark:border-red-700 p-4 rounded-lg"
        >
          <p className="font-semibold mb-1">Log stream unavailable</p>
          <p className="text-sm font-mono break-words">{error}</p>
          <p className="text-sm mt-2">
            The /api/logs endpoint was reachable but returned an error. See the
            status and detail above.
          </p>
        </div>
      ) : entries.length === 0 ? (
        <div className="bg-elevated rounded-lg shadow-sm p-6">
          <p className="text-sm text-muted">
            No log entries captured yet.
            {level
              ? ' The level filter is set and nothing matches.'
              : ''}{' '}
            Rows appear here as soon as the backend emits a log record.
          </p>
        </div>
      ) : (
        <div className="bg-elevated rounded-lg shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 dark:bg-gray-900/40 text-muted">
              <tr>
                <th scope="col" className="text-left font-medium px-4 py-2.5 w-44">
                  Time
                </th>
                <th scope="col" className="text-left font-medium px-4 py-2.5 w-28">
                  Level
                </th>
                <th scope="col" className="text-left font-medium px-4 py-2.5 w-56">
                  Logger
                </th>
                <th scope="col" className="text-left font-medium px-4 py-2.5">
                  Message
                </th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry, i) => {
                const s = LEVEL_STYLE[entry.level] ?? LEVEL_STYLE.info
                return (
                  <tr
                    key={`${entry.timestamp}-${i}`}
                    className="border-t border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-900/40 align-top"
                  >
                    <td className="px-4 py-2.5 whitespace-nowrap text-muted font-mono text-xs tabular-nums">
                      {formatTime(entry.timestamp)}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-xs font-medium ${s.bg} ${s.ring} ${s.text}`}
                        aria-label={`Level: ${entry.level}`}
                      >
                        <span className={`w-1.5 h-1.5 rounded-full ${s.dot}`} aria-hidden="true" />
                        {entry.level}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-secondary break-all">
                      {entry.logger}
                    </td>
                    <td className="px-4 py-2.5 text-secondary break-words whitespace-pre-wrap font-mono text-xs leading-relaxed">
                      {entry.message}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default LogsView
