/**
 * Connection Center — diagnostics content (acceptance row 13: a failed
 * connector must expose an error code and a plain-language recovery
 * action).
 *
 * Pure module (no React, no fetch): `summarizeDiagnostics` maps the frozen
 * `GET /api/connections/{id}/diagnostics` payload
 * (backend `ConnectionControlPlane.diagnostics`) to the exact content the
 * panel renders, so the panel's output is unit-testable against failed
 * connector fixtures.
 */

import type { ConnectionDiagnostics } from '../../api/connections'
import { formatRelative } from './stateMapping'

export interface DiagnosticsJobSummary {
  id: string
  state: string
  error_code: string | null
  started_at: string | null
  finished_at: string | null
}

export interface DiagnosticsSummary {
  /** Stable machine-readable error code (null when healthy). */
  error_code: string | null
  /** The plain-language fix from the backend's frozen diagnostics map. */
  fix: string
  last_attempt: { relative: string; absolute: string } | null
  last_success: { relative: string; absolute: string } | null
  job: DiagnosticsJobSummary | null
  /** Whether this connection currently has a recorded failure. */
  hasError: boolean
}

export function summarizeDiagnostics(d: ConnectionDiagnostics): DiagnosticsSummary {
  const code = d.diagnosis?.error_code ?? d.last_error_code ?? null
  return {
    error_code: code,
    fix: d.diagnosis?.fix ?? 'No errors recorded for this connection.',
    last_attempt: formatRelative(d.diagnosis?.last_attempt_at ?? d.last_attempt_at),
    last_success: formatRelative(d.diagnosis?.last_success_at ?? d.last_success_at),
    job: d.diagnosis?.import_job
      ? {
          id: d.diagnosis.import_job.id,
          state: d.diagnosis.import_job.state,
          error_code: d.diagnosis.import_job.error_code,
          started_at: d.diagnosis.import_job.started_at,
          finished_at: d.diagnosis.import_job.finished_at,
        }
      : null,
    hasError: code !== null,
  }
}
