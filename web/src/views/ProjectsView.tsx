import { useCallback, useEffect, useState } from 'react'
import { projectsApi, type OpenAIStatus, type ProjectDetail, type ProjectSummary } from '../api/projects'
import { modelPolicyApi, type ModelPolicy } from '../api/modelPolicy'
import { workbenchApi, type WorkbenchUsage } from '../api/workbench'

/**
 * ProjectsView (NS-ADR-104): shared project context. Notes are append-only and
 * show who wrote them (you, an agent, ChatGPT or the OpenAI API client).
 */
const input = 'mt-1 w-full rounded border border-border bg-transparent p-1 text-sm'

function Detail({ slug, openai, onChanged }: { slug: string; openai: OpenAIStatus | null; onChanged: () => void }) {
  const [d, setD] = useState<ProjectDetail | null>(null)
  const [summary, setSummary] = useState('')
  const [note, setNote] = useState('')
  const [prompt, setPrompt] = useState('')
  const [answer, setAnswer] = useState<string | null>(null)
  const [answeredBy, setAnsweredBy] = useState<string | null>(null)
  const [useOpenai, setUseOpenai] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const detail = await projectsApi.detail(slug)
      setD(detail)
      setSummary((s) => (s === '' ? detail.summary : s))
      setErr(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not load the project')
    }
  }, [slug])

  useEffect(() => {
    void load()
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
  return (
    <div className="mt-3 space-y-3 text-sm text-secondary" data-testid="proj-detail">
      <label className="block text-xs">Summary
        <textarea className={input} rows={3} value={summary} onChange={(e) => setSummary(e.target.value)} />
      </label>
      <button type="button" onClick={() => void act(() => projectsApi.saveSummary(slug, summary))}
        className="rounded border border-border px-3 py-1">Save summary</button>
      <ol className="space-y-1" data-testid="proj-notes">
        {d.notes.map((n) => (
          <li key={n.id}><span className="font-semibold">{n.author}</span>: {n.body}</li>
        ))}
        {d.notes.length === 0 && <li className="text-xs text-muted">No notes yet.</li>}
      </ol>
      <label className="block text-xs">Add a note
        <textarea className={input} rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <button type="button" disabled={!note.trim()}
        onClick={() => void act(async () => { await projectsApi.addNote(slug, note.trim()); setNote('') })}
        className="rounded border border-border px-3 py-1 disabled:opacity-50">Add note</button>
      <section data-testid="proj-ask">
        <h3 className="font-semibold text-primary">Ask with this context</h3>
        <p className="text-xs text-muted">Local models answer first. OpenAI is used only if you tick the box below or the local models fail.</p>
        <textarea aria-label="Question" className={input} rows={2} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        <label className="block text-xs">
          <input type="checkbox" checked={useOpenai} disabled={!!openai && !openai.key_configured}
            onChange={(e) => setUseOpenai(e.target.checked)} /> Use OpenAI for this question
          {openai && !openai.key_configured && <span data-testid="proj-openai-off"> (OpenAI is not configured)</span>}
        </label>
        <button type="button" disabled={!prompt.trim()}
          onClick={() => void act(async () => {
            const out = await projectsApi.ask(slug, prompt.trim(), useOpenai)
            setAnswer(out.content)
            setAnsweredBy(out.engine === 'local' ? `Local (${out.model})` : `OpenAI (${out.reason})`)
          })}
          className="rounded border border-border px-3 py-1 disabled:opacity-50">Ask</button>
        {answer && <pre className="mt-2 whitespace-pre-wrap text-sm" data-testid="proj-answer">{answer}</pre>}
        {answeredBy && <p className="text-xs text-muted" data-testid="proj-answered-by">Answered by {answeredBy}</p>}
      </section>
      {err && <p role="alert" className="text-danger">{err}</p>}
    </div>
  )
}

function NewProject({ onCreated }: { onCreated: () => void }) {
  const [slug, setSlug] = useState('')
  const [title, setTitle] = useState('')
  const [summary, setSummary] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const save = async () => {
    try {
      await projectsApi.create({ slug: slug.trim(), title: title.trim(), summary })
      setSlug('')
      setTitle('')
      setSummary('')
      setErr(null)
      onCreated()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not create the project')
    }
  }
  return (
    <div className="grid grid-cols-2 gap-2 text-xs text-secondary" data-testid="proj-new">
      <label>Short name (lowercase, dashes)<input className={input} value={slug} onChange={(e) => setSlug(e.target.value)} /></label>
      <label>Title<input className={input} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="col-span-2">Summary<textarea className={input} rows={2} value={summary} onChange={(e) => setSummary(e.target.value)} /></label>
      <button type="button" disabled={!slug.trim() || !title.trim()} onClick={save}
        className="col-span-2 rounded border border-border px-3 py-1 text-sm disabled:opacity-50">Create project</button>
      {err && <p role="alert" className="col-span-2 text-sm text-danger">{err}</p>}
    </div>
  )
}

function ModelUse() {
  const [p, setP] = useState<ModelPolicy | null>(null)
  const [usage, setUsage] = useState<WorkbenchUsage[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [kind, setKind] = useState('agent')
  const [id, setId] = useState('')
  const [mode, setMode] = useState('high_stakes')
  const load = useCallback(async () => {
    try {
      setP(await modelPolicyApi.get())
      // Session usage exists only at workbench phase 2+; absence is not an error.
      setUsage((await workbenchApi.listUsage(undefined, 20).catch(() => ({ usage: [] }))).usage)
      setErr(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not load model use')
    }
  }, [])
  useEffect(() => {
    void load()
    const t = setInterval(() => void load(), 15000)
    return () => clearInterval(t)
  }, [load])
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn()
      await load()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Action failed')
    }
  }
  if (!p) return err ? <p role="alert" className="text-sm text-danger">{err}</p> : null
  const o = p.openai
  return (
    <section className="rounded border border-border p-4 text-sm text-secondary" data-testid="model-use">
      <h2 className="text-sm font-semibold text-primary">Model use</h2>
      <p>Local first: {p.local_model}. OpenAI only when you ask, for a high-stakes task, or after {p.max_local_failures} local failures.</p>
      <p data-testid="openai-usage">
        OpenAI {o.key_configured ? `(${o.model_id})` : '(not configured)'}: today {o.today.requests} requests,{' '}
        {o.today.input_tokens + o.today.output_tokens} tokens, {o.today.throttled} rate-limited. Queue {o.queue_depth},
        running {o.in_flight}.{o.next_slot_at ? ` Next slot ${o.next_slot_at.slice(11, 19)} UTC.` : ''}
      </p>
      <p className="text-xs text-muted">
        Limits: {o.limits.concurrency} at a time, {o.limits.min_spacing_s}s apart, {o.limits.max_requests_hour}/h, {o.limits.max_requests_day}/day.
      </p>
      {usage.length > 0 && (
        <>
          <h3 className="mt-2 font-semibold text-primary">Session usage</h3>
          <ul data-testid="session-usage">
            {usage.map((u) => (
              <li key={u.id}>
                Session {u.session_id}: {u.model}, {u.input_tokens} in / {u.output_tokens} out tokens
              </li>
            ))}
          </ul>
        </>
      )}
      <h3 className="mt-2 font-semibold text-primary">OpenAI permissions</h3>
      <ul data-testid="grants">
        {p.grants.length === 0 && <li className="text-xs text-muted">None. Everything stays local.</li>}
        {p.grants.map((g) => (
          <li key={`${g.subject_kind}:${g.subject_id}`}>
            {g.subject_kind} {g.subject_id}: {g.mode === 'owner_requested' ? 'always use OpenAI' : 'high stakes (OpenAI after local fails)'}{' '}
            <button type="button" className="text-xs underline" onClick={() => void act(() => modelPolicyApi.removeGrant(g.subject_kind, g.subject_id))}>Remove</button>
          </li>
        ))}
      </ul>
      <div className="mt-2 flex flex-wrap gap-2 text-xs">
        <select aria-label="Kind" className="rounded border border-border bg-transparent" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="agent">agent</option><option value="project">project</option><option value="handoff">handoff</option>
        </select>
        <input aria-label="Id" placeholder="agent id or project slug" className="rounded border border-border bg-transparent px-1" value={id} onChange={(e) => setId(e.target.value)} />
        <select aria-label="Mode" className="rounded border border-border bg-transparent" value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="high_stakes">high stakes</option><option value="owner_requested">always use OpenAI</option>
        </select>
        <button type="button" disabled={!id.trim()} className="rounded border border-border px-2 disabled:opacity-50"
          onClick={() => void act(async () => { await modelPolicyApi.setGrant({ subject_kind: kind as 'agent', subject_id: id.trim(), mode: mode as 'high_stakes' }); setId('') })}>Allow</button>
      </div>
      {err && <p role="alert" className="text-danger">{err}</p>}
    </section>
  )
}

export default function ProjectsView() {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [openai, setOpenai] = useState<OpenAIStatus | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setProjects(await projectsApi.list())
      setOpenai(await projectsApi.openaiStatus().catch(() => null))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load projects')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="space-y-4 p-4">
      <h1 className="text-lg font-semibold text-primary">Projects</h1>
      <p className="text-xs text-muted">
        Shared project context. Notes are append-only and name who wrote them. The same context
        goes to OpenAI when you ask a question here.
      </p>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <section className="rounded border border-border p-4" data-testid="proj-list">
        <h2 className="text-sm font-semibold text-primary">Your projects</h2>
        {projects.length === 0 && <p className="text-xs text-muted">No projects yet.</p>}
        <ul className="mt-2 space-y-1 text-sm text-secondary">
          {projects.map((p) => (
            <li key={p.slug}>
              <button type="button" onClick={() => setSelected(selected === p.slug ? null : p.slug)} className="underline">
                {p.title}
              </button>{' '}
              ({p.note_count ?? 0} notes)
              {selected === p.slug && <Detail slug={p.slug} openai={openai} onChanged={load} />}
            </li>
          ))}
        </ul>
      </section>
      <section className="rounded border border-border p-4">
        <h2 className="text-sm font-semibold text-primary">New project</h2>
        <NewProject onCreated={load} />
      </section>
      <ModelUse />
    </div>
  )
}
