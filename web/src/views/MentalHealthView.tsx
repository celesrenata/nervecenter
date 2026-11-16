import { useCallback, useEffect, useState } from 'react'
import {
  mentalHealthApi,
  type MhCorrelations,
  type MhInstrument,
  type MhInstrumentTrend,
  type MhTrends,
} from '../api/mentalHealth'

/**
 * MentalHealthView (NS-ADR-101): daily PHQ-9 and GAD-7, score charts with
 * 7/30-day trendlines, and lagged associations against health, sleep,
 * spending and calendar load. Data stays local. Associations are not causes.
 */

function Chart({ trend }: { trend: MhInstrumentTrend }) {
  if (trend.daily.length < 2) return <p className="text-xs text-muted">Not enough days for a chart yet.</p>
  const w = 360
  const h = 120
  const days = trend.daily.map((p) => p.date)
  const x = (d: string) => (days.indexOf(d) / (days.length - 1)) * (w - 8) + 4
  const y = (v: number) => h - 6 - (v / trend.max) * (h - 12)
  const line = (pts: { date: string; value: number }[]) =>
    pts.filter((p) => days.includes(p.date)).map((p) => `${x(p.date)},${y(p.value)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label={`${trend.title} scores over time`}>
      <polyline points={line(trend.daily)} fill="none" stroke="var(--muted, #888)" strokeWidth={1} />
      <polyline points={line(trend.rolling_7)} fill="none" stroke="var(--accent)" strokeWidth={2} />
      <polyline points={line(trend.rolling_30)} fill="none" stroke="var(--primary, #ccc)" strokeWidth={2} strokeDasharray="4 3" />
    </svg>
  )
}

function Form({ inst, onSaved }: { inst: MhInstrument; onSaved: () => void }) {
  const [answers, setAnswers] = useState<(number | null)[]>(() => inst.items.map(() => null))
  const [comment, setComment] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const complete = answers.every((a) => a !== null)
  const save = async () => {
    try {
      const r = await mentalHealthApi.submit({
        instrument: inst.key,
        answers: answers as number[],
        comment: comment.trim() || undefined,
      })
      setMsg(`Saved: ${r.total} / ${r.max} (${r.band})${r.created ? '' : ', replaced today’s entry'}`)
      setErr(null)
      onSaved()
    } catch (e) {
      setMsg(null)
      setErr(e instanceof Error ? e.message : 'Could not save')
    }
  }
  return (
    <section className="rounded border border-border p-4" data-testid={`mh-form-${inst.key}`}>
      <h2 className="text-sm font-semibold text-primary">{inst.title}</h2>
      <p className="text-xs text-muted">{inst.prompt}</p>
      <ol className="mt-3 space-y-3">
        {inst.items.map((item, i) => (
          <li key={item}>
            <p className="text-sm text-secondary">{item}</p>
            <div className="mt-1 flex flex-wrap gap-3" role="radiogroup" aria-label={item}>
              {inst.choices.map((c) => (
                <label key={c.value} className="text-xs text-secondary">
                  <input
                    type="radio"
                    name={`${inst.key}-${i}`}
                    checked={answers[i] === c.value}
                    onChange={() => setAnswers((a) => a.map((v, j) => (j === i ? c.value : v)))}
                  />{' '}
                  {c.label}
                </label>
              ))}
            </div>
          </li>
        ))}
      </ol>
      <label className="mt-3 block text-xs text-secondary">
        Write-in (optional)
        <textarea
          value={comment}
          maxLength={4000}
          onChange={(e) => setComment(e.target.value)}
          className="mt-1 w-full rounded border border-border bg-transparent p-2 text-sm"
          rows={3}
        />
      </label>
      <button
        type="button"
        disabled={!complete}
        onClick={save}
        className="mt-3 rounded border border-border px-3 py-1 text-sm disabled:opacity-50"
      >
        Save {inst.title}
      </button>
      {msg && <p className="mt-2 text-sm text-secondary" data-testid={`mh-saved-${inst.key}`}>{msg}</p>}
      {err && <p role="alert" className="mt-2 text-sm text-danger">{err}</p>}
    </section>
  )
}

function patterns(c: MhCorrelations): string[] {
  return c.rows
    .filter((r) => r.state === 'ok' && r.r !== null && Math.abs(r.r) >= 0.3)
    .slice(0, 4)
    .map((r) => {
      const dir = (r.r ?? 0) > 0 ? 'higher' : 'lower'
      const when = r.lag === 0 ? 'the same day' : `${r.lag} day${r.lag === 1 ? '' : 's'} earlier`
      return `${r.instrument === 'phq9' ? 'PHQ-9' : 'GAD-7'} scores tend to be ${dir} when ${r.label.toLowerCase()} was higher ${when} (r ${r.r?.toFixed(2)}, n ${r.n}).`
    })
}

export default function MentalHealthView() {
  const [insts, setInsts] = useState<MhInstrument[]>([])
  const [trends, setTrends] = useState<MhTrends | null>(null)
  const [corr, setCorr] = useState<MhCorrelations | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    Promise.all([mentalHealthApi.instruments(), mentalHealthApi.trends(90), mentalHealthApi.correlations(90)])
      .then(([i, t, c]) => {
        setInsts(i)
        setTrends(t)
        setCorr(c)
        setError(null)
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [])
  useEffect(load, [load])

  return (
    <div className="space-y-4 p-6">
      <h1 className="text-xl font-semibold text-primary">Mental Health</h1>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <div className="grid gap-4 lg:grid-cols-2">{insts.map((i) => <Form key={i.key} inst={i} onSaved={load} />)}</div>

      <div className="grid gap-4 lg:grid-cols-2">
        {trends &&
          Object.entries(trends.instruments).map(([key, t]) => (
            <section key={key} className="rounded border border-border p-4" data-testid={`mh-trend-${key}`}>
              <h2 className="text-sm font-semibold text-primary">{t.title} over time</h2>
              {t.latest ? (
                <p className="text-sm text-secondary">
                  Latest {t.latest.total} / {t.max} ({t.latest.band}) on {t.latest.date} · {t.count} days logged
                </p>
              ) : (
                <p className="text-sm text-muted">No entries yet.</p>
              )}
              <Chart trend={t} />
              <p className="text-xs text-muted">Solid line: 7-day average. Dashed: 30-day average. Grey: daily.</p>
              {t.change_point && (
                <p className="text-xs text-secondary">
                  Biggest shift around {t.change_point.date}: average {t.change_point.before_mean} before, {t.change_point.after_mean} after.
                </p>
              )}
              {t.streaks.longest > 0 && (
                <p className="text-xs text-secondary">
                  Days at {t.streaks.threshold} or above: longest run {t.streaks.longest}, current {t.streaks.current}.
                </p>
              )}
            </section>
          ))}
      </div>

      {corr && (
        <section className="rounded border border-border p-4" data-testid="mh-factors">
          <h2 className="text-sm font-semibold text-primary">What moves with your scores</h2>
          <p className="text-xs text-muted">{corr.note}</p>
          <div className="mt-2 space-y-1" data-testid="mh-patterns">
            {patterns(corr).map((p) => (
              <p key={p} className="rounded bg-surface p-2 text-sm text-secondary">{p}</p>
            ))}
          </div>
          <table className="mt-3 w-full text-left text-sm">
            <thead>
              <tr className="text-xs text-muted">
                <th>Score</th><th>Factor</th><th>r</th><th>Lag (days)</th><th>n</th>
              </tr>
            </thead>
            <tbody>
              {corr.rows.map((r) => (
                <tr key={`${r.instrument}-${r.factor}`} data-testid={`mh-row-${r.instrument}-${r.factor}`}>
                  <td>{r.instrument === 'phq9' ? 'PHQ-9' : 'GAD-7'}</td>
                  <td>{r.label}</td>
                  {r.state === 'ok' ? (
                    <>
                      <td>{r.r?.toFixed(2)}</td><td>{r.lag}</td><td>{r.n}</td>
                    </>
                  ) : (
                    <td colSpan={3} className="text-muted">Too few samples ({r.n} of {r.min_samples} days)</td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  )
}
