/**
 * Connection Center — diagnostics panel (acceptance row 13).
 *
 * Fetches `GET /api/connections/{id}/diagnostics` on open and renders the
 * frozen payload: the stable error code, the plain-language recovery fix,
 * attempt/success timestamps, and the last import job's state. The content
 * mapping itself lives in `diagnostics.ts` (unit-tested); this component is
 * a thin presentational layer around it.
 */

import { useEffect, useState } from 'react'
import { connectionsApi, type ConnectionDiagnostics } from '../../api/connections'
import { summarizeDiagnostics } from './diagnostics'

export function ConnectionDiagnostics({
  connectionId,
  refreshKey,
}: {
  connectionId: string
  /** Changes when the parent row is refreshed — re-fetch the diagnostics. */
  refreshKey?: string | null
}) {
  const [data, setData] = useState<ConnectionDiagnostics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    connectionsApi
      .getDiagnostics(connectionId)
      .then((result) => {
        if (cancelled) return
        setData(result)
        setLoading(false)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(errMessage(err))
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [connectionId, refreshKey])

  if (loading) {
    return (
      <div className="rounded-md border border-line bg-hover/40 p-4 text-sm text-muted" role="status">
        Loading diagnostics…
      </div>
    )
  }

  if (error || !data) {
    return (
      <div
        className="rounded-md border border-[color-mix(in_srgb,var(--danger)_40%,transparent)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] p-4 text-sm text-danger"
        role="alert"
      >
        Diagnostics unavailable: {error ?? 'unknown error'}
      </div>
    )
  }

  const summary = summarizeDiagnostics(data)
  return (
    <div className="rounded-md border border-line bg-hover/40 p-4 space-y-3 text-sm" aria-live="polite">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Diagnostics</h4>

      {summary.hasError ? (
        <>
          <p className="text-secondary leading-relaxed">{summary.fix}</p>
          <p className="text-xs text-danger">
            Error code: <code className="font-mono">{summary.error_code}</code>
          </p>
        </>
      ) : (
        <p className="text-secondary">{summary.fix}</p>
      )}

      <dl className="text-xs text-muted space-y-1">
        <div>
          <dt className="inline">Last attempt: </dt>
          <dd className="inline">
            {summary.last_attempt
              ? `${summary.last_attempt.relative} (${summary.last_attempt.absolute})`
              : 'never'}
          </dd>
        </div>
        <div>
          <dt className="inline">Last success: </dt>
          <dd className="inline">
            {summary.last_success
              ? `${summary.last_success.relative} (${summary.last_success.absolute})`
              : 'never ingested'}
          </dd>
        </div>
      </dl>

      {summary.job && (
        <p className="text-xs text-muted">
          Import job <code className="font-mono">{summary.job.id.slice(0, 8)}…</code>{' '}
          — state: {summary.job.state}
          {summary.job.error_code && (
            <span className="text-danger"> · error: {summary.job.error_code}</span>
          )}
        </p>
      )}
    </div>
  )
}

function errMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'detail' in err) {
    const detail = (err as { detail: unknown }).detail
    if (typeof detail === 'string' && detail) return detail
  }
  if (err instanceof Error && err.message) return err.message
  return 'request failed'
}
