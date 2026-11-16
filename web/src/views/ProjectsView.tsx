import { useCallback, useEffect, useState } from 'react'
import { projectsApi, type OpenAIStatus, type ProjectDetail, type ProjectSummary } from '../api/projects'

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
        <h3 className="font-semibold text-primary">Ask OpenAI with this context</h3>
        {openai && !openai.key_configured ? (
          <p className="text-xs text-muted" data-testid="proj-openai-off">
            OpenAI is not configured. Add the NERVE_OPENAI_KEY secret to turn this on.
          </p>
        ) : (
          <>
            <textarea aria-label="Question" className={input} rows={2} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
            <button type="button" disabled={!prompt.trim()}
              onClick={() => void act(async () => setAnswer((await projectsApi.ask(slug, prompt.trim())).content))}
              className="rounded border border-border px-3 py-1 disabled:opacity-50">Ask</button>
            {answer && <pre className="mt-2 whitespace-pre-wrap text-sm" data-testid="proj-answer">{answer}</pre>}
          </>
        )}
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
    </div>
  )
}
