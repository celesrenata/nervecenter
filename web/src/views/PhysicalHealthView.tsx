import { useEffect, useState } from 'react'
import { healthApi, type HealthKindSummary, type HealthSummary } from '../api/health'

/**
 * PhysicalHealthView — Apple Health data pushed by the iPhone app (NS-ADR-100).
 * Weight, sleep, steps and heart rate as daily trend lines (UTC days). Shows
 * an honest empty state until the phone has synced. No advice, only data.
 */

const CARDS: { kind: string; title: string; digits: number }[] = [
  { kind: 'body_mass', title: 'Weight', digits: 1 },
  { kind: 'sleep', title: 'Sleep (asleep)', digits: 0 },
  { kind: 'step_count', title: 'Steps', digits: 0 },
  { kind: 'heart_rate', title: 'Heart rate', digits: 0 },
  { kind: 'resting_heart_rate', title: 'Resting heart rate', digits: 0 },
  { kind: 'hrv', title: 'HRV', digits: 0 },
  { kind: 'active_energy', title: 'Active energy', digits: 0 },
]

function Trend({ points }: { points: number[] }) {
  if (points.length < 2) return <p className="text-xs text-muted">Not enough days for a trend yet.</p>
  const w = 220
  const h = 48
  const lo = Math.min(...points)
  const span = Math.max(...points) - lo || 1
  const step = w / (points.length - 1)
  const line = points
    .map((v, i) => `${i * step},${h - 4 - ((v - lo) / span) * (h - 8)}`)
    .join(' ')
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h} role="img" aria-label="daily trend">
      <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth={2} />
    </svg>
  )
}

function fmt(v: number | null | undefined, digits: number): string {
  return v == null ? '—' : v.toLocaleString(undefined, { maximumFractionDigits: digits })
}

function Card({ title, digits, data }: { title: string; digits: number; data: HealthKindSummary }) {
  return (
    <section className="rounded border border-border p-4" data-testid={`health-card-${title}`}>
      <h2 className="text-sm font-semibold text-primary">{title}</h2>
      {data.count === 0 ? (
        <p className="mt-2 text-sm text-muted">No samples in this window.</p>
      ) : (
        <>
          <p className="mt-1 text-2xl text-primary">
            {fmt(data.latest?.value, digits)} <span className="text-sm text-muted">{data.unit}</span>
          </p>
          <p className="text-xs text-muted">
            avg {fmt(data.mean, digits)} · min {fmt(data.min, digits)} · max {fmt(data.max, digits)} ·{' '}
            {data.count} samples
          </p>
          <div className="mt-2">
            <Trend points={data.daily.map((d) => d.value)} />
          </div>
        </>
      )}
    </section>
  )
}

export default function PhysicalHealthView() {
  const [days, setDays] = useState(30)
  const [summary, setSummary] = useState<HealthSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    healthApi
      .getSummary(days)
      .then((s) => live && (setSummary(s), setError(null)))
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : 'Could not load health data'))
    return () => {
      live = false
    }
  }, [days])

  return (
    <div className="space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-primary">Physical Health</h1>
        <label className="text-sm text-secondary">
          Window{' '}
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded border border-border bg-transparent px-2 py-1"
          >
            {[7, 30, 90, 180].map((d) => (
              <option key={d} value={d}>
                {d} days
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      {summary && !summary.configured && (
        <p className="text-sm text-muted">Durable storage is not configured, so no health data is kept.</p>
      )}
      {summary && summary.configured && summary.total_samples === 0 && (
        <p className="text-sm text-muted" data-testid="health-empty">
          No Apple Health data yet. Pair the iPhone app and run Sync now (see apple/INSTALL-iphone.md).
        </p>
      )}
      {summary && summary.last_sync_at && (
        <p className="text-xs text-muted">
          Last data received {new Date(summary.last_sync_at).toLocaleString()} · {summary.total_samples} samples
        </p>
      )}
      {summary && summary.total_samples > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          {CARDS.map((c) => (
            <Card key={c.kind} title={c.title} digits={c.digits} data={summary.kinds[c.kind]} />
          ))}
        </div>
      )}
    </div>
  )
}
