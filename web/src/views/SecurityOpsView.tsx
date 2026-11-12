/**
 * SecurityOpsView — NerveShield Task 7.7 operations dashboard.
 *
 * Read-only. Shows pipeline health, review backlog, ArcForge service status,
 * model/feed/policy versions, model-run counts and timings, false-positive
 * review figures, gate holds, confirmations and runner failure counters.
 * Everything is counts, version labels and timings from the server's
 * allow-listed projection: no message, account or person identifiers, no
 * free text, no remote resources. All values render as inert text.
 */

import { useCallback, useEffect, useState } from 'react'
import { mailSecurityApi, type OpsDashboard } from '../api/mailSecurity'

function Section({
  title,
  testId,
  children,
}: {
  title: string
  testId: string
  children: React.ReactNode
}) {
  return (
    <section
      data-testid={testId}
      aria-labelledby={`${testId}-h`}
      className="rounded-lg border border-line bg-elevated px-4 py-3"
    >
      <h2 id={`${testId}-h`} className="text-sm font-semibold text-primary">
        {title}
      </h2>
      <div className="mt-2 text-xs text-secondary">{children}</div>
    </section>
  )
}

function CountTable({ rows, empty = 'None' }: { rows: Record<string, number>; empty?: string }) {
  const entries = Object.entries(rows)
  if (entries.length === 0) return <p className="text-muted">{empty}</p>
  return (
    <table className="w-full text-left">
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k} className="border-t border-line first:border-t-0">
            <td className="py-0.5 pr-2 font-mono break-all">{k}</td>
            <td className="py-0.5 text-right tabular-nums">{String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function fmt(n: number | null | undefined, digits = 1): string {
  return typeof n === 'number' && Number.isFinite(n) ? n.toFixed(digits) : '—'
}

export function SecurityOpsView() {
  const [ops, setOps] = useState<OpsDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(false)
    try {
      setOps(await mailSecurityApi.getOps())
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const fb = ops?.counts.feedback
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-3xl font-bold text-primary">Security ops</h1>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded border border-line px-3 py-1 text-sm text-secondary hover:bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
        >
          Refresh
        </button>
      </div>
      {loading && <p className="text-sm text-muted">Loading…</p>}
      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-900/30 dark:text-red-300"
        >
          The dashboard could not be loaded.
        </div>
      )}
      {!loading && !error && ops === null && (
        <div data-testid="ops-empty" className="rounded-lg border border-line p-4 text-sm text-muted">
          No security data available for your accounts.
        </div>
      )}
      {ops && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Section title="Pipeline health" testId="ops-health">
            <ul className="space-y-0.5">
              {ops.health.stages.map((s) => (
                <li key={s.stage} className="flex justify-between gap-2">
                  <span className="font-mono">{s.stage}</span>
                  <span>{s.status}</span>
                </li>
              ))}
            </ul>
            {ops.health.alerts.length > 0 && (
              <p className="mt-2">{`Alerts: ${ops.health.alerts.map((a) => a.code).join(', ')}`}</p>
            )}
          </Section>

          <Section title="Review queue" testId="ops-queue">
            <p>{`${ops.review_queue.pending} pending · ${ops.review_queue.digest} grouped · ${ops.review_queue.reviewed} reviewed`}</p>
            {ops.review_queue.quiet_hours_active && <p>Quiet hours are active.</p>}
            <CountTable rows={ops.review_queue.by_tier} />
          </Section>

          <Section title="Arc service" testId="ops-arc">
            <p>{`Overall: ${ops.arc.status}`}</p>
            {ops.arc.status === 'NOT_CONFIGURED' && (
              <p className="text-muted">No ArcForge target is configured.</p>
            )}
            {(ops.arc.hosts ?? []).map((h) => (
              <div key={h.label} className="mt-2" data-testid={`ops-arc-${h.label}`}>
                <p className="font-semibold">{`${h.label}: ${h.status}`}</p>
                {h.model && <p>{`${h.model.model_id ?? '—'} (${h.model.version ?? '—'}) on ${h.model.device ?? '—'}`}</p>}
                {h.activity && (
                  <p>
                    {`Queue ${fmt(h.activity.queue_depth, 0)} · active ${fmt(h.activity.active_inferences, 0)} · requests ${fmt(h.activity.requests_total, 0)} · mean ${fmt(h.activity.mean_inference_seconds, 2)} s${h.activity.device_wedged ? ' · device wedged' : ''}`}
                  </p>
                )}
              </div>
            ))}
            <p className="mt-2 text-muted">Service activity, not a GPU-percent measure.</p>
          </Section>

          <Section title="False-positive review" testId="ops-feedback">
            <p>{`Flagged verdicts called safe: ${fb?.flagged_false_positive_rate == null ? '—' : `${Math.round(fb.flagged_false_positive_rate * 100)}%`}`}</p>
            <p>{`Missed-threat reports: ${fb?.missed_threat_reports ?? 0}`}</p>
            <CountTable rows={fb?.by_kind ?? {}} />
          </Section>

          <Section title="Model runs (count, mean / max ms)" testId="ops-models">
            {Object.keys(ops.counts.model_runs ?? {}).length === 0 ? (
              <p className="text-muted">None</p>
            ) : (
              <ul className="space-y-0.5">
                {Object.entries(ops.counts.model_runs ?? {}).map(([k, v]) => (
                  <li key={k} className="font-mono break-all">
                    {`${k}: ${v.count} · ${fmt(v.latency_ms_mean, 0)} / ${fmt(v.latency_ms_max, 0)}`}
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Versions (profile | policy | model)" testId="ops-versions">
            <CountTable rows={ops.counts.versions ?? {}} />
            <ul className="mt-2 space-y-0.5">
              {ops.health.intel_feeds.map((f) => (
                <li key={f.feed} className="font-mono">{`${f.feed} ${f.feed_version ?? '—'} · ${f.live_indicators} live`}</li>
              ))}
            </ul>
          </Section>

          <Section title="Gate holds and confirmations" testId="ops-gate">
            <CountTable rows={ops.counts.gate_holds ?? {}} empty="No holds" />
            <div className="mt-2">
              <CountTable rows={ops.counts.confirmations ?? {}} empty="No confirmations" />
            </div>
          </Section>

          <Section title="Runner failures" testId="ops-runner">
            <p>{ops.runner.running ? 'Runner is running.' : 'Runner is not running in this process.'}</p>
            <CountTable rows={ops.runner.failures} empty="No failures" />
          </Section>
        </div>
      )}
    </div>
  )
}
