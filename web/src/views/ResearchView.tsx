import { useCallback, useEffect, useState } from 'react'
import { researchApi, type ResearchDetail, type ResearchProject } from '../api/research'

/**
 * ResearchView (NS-ADR-103): long-running research projects that work through
 * a plan one checkpointed step at a time on the `secure/planner` combo. Shows
 * progress, the step results, your notes and the final report.
 */
const STATUS_LABEL: Record<string, string> = {
  planned: 'Waiting to plan',
  running: 'Running',
  paused: 'Paused',
  done: 'Done',
  failed: 'Failed',
}

function NewProject({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState('')
  const [question, setQuestion] = useState('')
  const [hours, setHours] = useState('4')
  const [cadence, setCadence] = useState('300')
  const [err, setErr] = useState<string | null>(null)
  const input = 'mt-1 w-full rounded border border-border bg-transparent p-1 text-sm'
  const save = async () => {
    try {
      await researchApi.create({
        title: title.trim(),
        question: question.trim(),
        budget_hours: Number(hours),
        cadence_seconds: Number(cadence),
      })
      setTitle('')
      setQuestion('')
      setErr(null)
      onCreated()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not create the project')
    }
  }
  return (
    <div className="grid grid-cols-2 gap-2 text-xs text-secondary" data-testid="res-new">
      <label className="col-span-2">Title<input className={input} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="col-span-2">Question<textarea className={input} rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} /></label>
      <label>Budget (hours)<input className={input} type="number" min="0.1" step="0.5" value={hours} onChange={(e) => setHours(e.target.value)} /></label>
      <label>Seconds between steps<input className={input} type="number" min="30" step="30" value={cadence} onChange={(e) => setCadence(e.target.value)} /></label>
      <button type="button" disabled={!title.trim() || !question.trim()} onClick={save}
        className="col-span-2 rounded border border-border px-3 py-1 text-sm disabled:opacity-50">Start project</button>
      {err && <p role="alert" className="col-span-2 text-sm text-danger">{err}</p>}
    </div>
  )
}

function Detail({ id, onChanged }: { id: string; onChanged: () => void }) {
  const [d, setD] = useState<ResearchDetail | null>(null)
  const [report, setReport] = useState<string | null>(null)
  const [notes, setNotes] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const load = useCallback(async () => {
    try {
      const detail = await researchApi.detail(id)
      setD(detail)
      setNotes((n) => (n === '' ? (detail.notes ?? '') : n))
      if (detail.status === 'done') setReport((await researchApi.report(id)).report)
      setErr(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not load the project')
    }
  }, [id])
  useEffect(() => {
    void load()
    const t = setInterval(() => void load(), 15000)
    return () => clearInterval(t)
  }, [load])
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn()
      await load()
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Action failed')
    }
  }
  if (!d) return err ? <p role="alert" className="text-sm text-danger">{err}</p> : <p className="text-xs text-muted">Loading...</p>
  const total = d.steps_total ?? d.steps.length
  const done = d.steps_done ?? 0
  const canPause = d.status === 'planned' || d.status === 'running'
  const canResume = d.status === 'paused' || d.status === 'failed'
  return (
    <div className="mt-3 space-y-3 text-sm text-secondary" data-testid="res-detail">
      <div>
        <progress aria-label="Progress" value={done} max={Math.max(total, 1)} className="w-full" />
        <p data-testid="res-progress">
          {STATUS_LABEL[d.status]}: {done} of {total} steps. Model {d.combo}.
          {d.next_step_at && d.status === 'running' && ` Next step after ${d.next_step_at.slice(0, 16).replace('T', ' ')}.`}
        </p>
        {d.error && <p role="alert" className="text-danger">Last error: {d.error}</p>}
      </div>
      <div className="flex gap-2">
        {canPause && <button type="button" onClick={() => void act(() => researchApi.pause(id))} className="rounded border border-border px-3 py-1">Pause</button>}
        {canResume && <button type="button" onClick={() => void act(() => researchApi.resume(id))} className="rounded border border-border px-3 py-1">Resume</button>}
      </div>
      <ol className="space-y-1" data-testid="res-steps">
        {d.steps.map((s) => (
          <li key={s.id}>
            <details>
              <summary>{s.ordinal + 1}. {s.goal} ({s.status})</summary>
              {s.result && <pre className="whitespace-pre-wrap text-xs">{s.result}</pre>}
            </details>
          </li>
        ))}
      </ol>
      {d.working_notes && (
        <details>
          <summary>Working notes (checkpoint at step {d.checkpoint_cursor})</summary>
          <pre className="whitespace-pre-wrap text-xs" data-testid="res-working-notes">{d.working_notes}</pre>
        </details>
      )}
      <label className="block text-xs">Your notes
        <textarea className="mt-1 w-full rounded border border-border bg-transparent p-1 text-sm" rows={3}
          value={notes} onChange={(e) => setNotes(e.target.value)} />
      </label>
      <button type="button" onClick={() => void act(() => researchApi.saveNotes(id, notes))}
        className="rounded border border-border px-3 py-1">Save notes</button>
      {report && (
        <section data-testid="res-report">
          <h3 className="font-semibold text-primary">Report</h3>
          <pre className="whitespace-pre-wrap text-sm">{report}</pre>
        </section>
      )}
      {err && <p role="alert" className="text-danger">{err}</p>}
    </div>
  )
}

export default function ResearchView() {
  const [projects, setProjects] = useState<ResearchProject[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(async () => {
    try {
      setProjects(await researchApi.list())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load research projects')
    }
  }, [])
  useEffect(() => {
    void load()
  }, [load])
  return (
    <div className="space-y-4 p-4">
      <h1 className="text-lg font-semibold text-primary">Research</h1>
      <p className="text-xs text-muted">
        Long-running projects. Each runs a plan one step at a time, saves a checkpoint after every step,
        and slows down when inference is busy.
      </p>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <section className="rounded border border-border p-4" data-testid="res-projects">
        <h2 className="text-sm font-semibold text-primary">Projects</h2>
        {projects.length === 0 && <p className="text-xs text-muted">No research projects yet.</p>}
        <ul className="mt-2 space-y-1 text-sm text-secondary">
          {projects.map((p) => (
            <li key={p.id}>
              <button type="button" onClick={() => setSelected(selected === p.id ? null : p.id)} className="underline">
                {p.title}
              </button>{' '}
              ({STATUS_LABEL[p.status]}, {p.steps_done ?? 0}/{p.steps_total ?? 0} steps)
              {selected === p.id && <Detail id={p.id} onChanged={load} />}
            </li>
          ))}
        </ul>
      </section>
      <section className="rounded border border-border p-4">
        <h2 className="text-sm font-semibold text-primary">New project</h2>
        <NewProject onCreated={load} />
      </section>
    </div>
  )
}
