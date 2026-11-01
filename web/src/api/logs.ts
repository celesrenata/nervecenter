/**
 * Live in-service logs API client (spec §49 — Log buffer + in-service
 * observability).
 *
 * Typed client for the frozen `backend.api.endpoints.logs` route:
 *
 *   GET /api/logs?limit=&level=
 *     → { data: LogEntry[], secrets_scrubbed: true, meta: { ... } }
 *
 * The ring buffer (see `backend/observability/log_buffer.py`) is populated
 * live from both the structlog and standard-library log paths, so there is
 * no "unwired service" 501 branch — every response is real captured data.
 *
 * Mock mode returns an honest empty buffer: a live log stream cannot be
 * faked without fabricating the backend's behavior, so the view renders its
 * empty state rather than invented rows.
 */

import { apiFetch, apiErrorFromResponse, useMockMode } from './client'

/** Normalized log level name (backend `LogLevel` values). */
export type LogLevel =
  | 'debug'
  | 'info'
  | 'warning'
  | 'error'
  | 'critical'

/** A single captured log row (mirrors the backend `LogEntry` model). */
export interface LogEntry {
  level: LogLevel
  /** ISO-8601 UTC timestamp, e.g. `2026-11-01T11:39:24.123456Z`. */
  timestamp: string
  /** Origin logger name, e.g. `uvicorn.access` or `backend.api.health`. */
  logger: string
  /** Human-readable message, already scrubbed of credential-looking values. */
  message: string
}

/** Envelope returned by `GET /api/logs`. */
export interface LogsResponse {
  data: LogEntry[]
  /**
   * Always `true` — the capture pipeline redacts credential-looking
   * patterns before a record is stored. Documented guarantee, not state.
   */
  secrets_scrubbed: boolean
  meta: {
    version: string
    request_id: string
    timestamp: string
    pagination?: Record<string, number>
  }
}

/** Query options for `GET /api/logs`. */
export interface LogsQuery {
  /** Maximum rows to return (backend clamps to 1000; default 200). */
  limit?: number
  /** Minimum severity filter, e.g. `warning`. Empty → no filter. */
  level?: LogLevel | ''
}

// ─── Real API implementation ─────────────────────────────────────────────────

export const realLogsApi = {
  getLogs: async (query: LogsQuery = {}): Promise<LogsResponse> => {
    const params = new URLSearchParams()
    if (query.limit !== undefined) params.set('limit', String(query.limit))
    if (query.level) params.set('level', query.level)
    const qs = params.toString()
    const res = await apiFetch(qs ? `/api/logs?${qs}` : '/api/logs')
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as LogsResponse
  },
}

// ─── Mock implementation (honest degradation — no fabricated rows) ───────────

export const mockLogsApi = {
  getLogs: async (): Promise<LogsResponse> => ({
    data: [],
    secrets_scrubbed: true,
    meta: {
      version: '1.0.0',
      request_id: 'mock',
      timestamp: new Date().toISOString(),
    },
  }),
}

export const logsApi = useMockMode ? mockLogsApi : realLogsApi
