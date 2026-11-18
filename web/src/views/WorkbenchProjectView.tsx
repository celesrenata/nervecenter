import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  workbenchApi,
  type WorkbenchIdeTaskStatus,
  type WorkbenchKind,
  type WorkbenchProjectDetail,
  type WorkbenchSession,
} from '../api/workbench'
import IdeFrame from '../components/workbench/IdeFrame'
import QrLink from '../components/workbench/QrLink'

export const TASK_STATUS_POLL_MS = 30_000

/**
 * WorkbenchProjectView: one project, its 10 most recent handoffs (newest first),
 * and archive / delete controls. Delete requires typing the slug (Req 2.9, 2.10, 2.11, 3.5, 3.13).
 */
const MAX_HANDOFFS = 10
const btn = 'rounded border border-border px-3 py-1 text-sm disabled:opacity-50'

const errMsg = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)

interface HandoffRow {
  id: string
  title: string
  status: string
  createdAt: string
}

function toRow(raw: unknown, i: number): HandoffRow {
  const h = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const str = (v: unknown) => (typeof v === 'string' ? v : '')
  return {
    id: str(h.id) || String(i),
    title: str(h.title),
    status: str(h.status),
    createdAt: str(h.created_at),
  }
}

/** Newest first, capped at 10. Entries without a timestamp keep their server order after dated ones. */
export function recentHandoffs(handoffs: unknown[] | undefined): HandoffRow[] {
  const rows = (handoffs ?? []).map(toRow)
  const time = (r: HandoffRow) => {
    const t = Date.parse(r.createdAt)
    return Number.isNaN(t) ? -Infinity : t
  }
  return rows
    .map((r, i) => ({ r, i }))
    .sort((a, b) => time(b.r) - time(a.r) || a.i - b.i)
    .map((x) => x.r)
    .slice(0, MAX_HANDOFFS)
}

export default function WorkbenchProjectView() {
  const { kind = '', slug = '' } = useParams<{ kind: string; slug: string }>()
  const navigate = useNavigate()
  const [project, setProject] = useState<WorkbenchProjectDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [sessions, setSessions] = useState<WorkbenchSession[]>([])
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [typed, setTyped] = useState('')

  const load = useCallback(async () => {
    try {
      setProject(await workbenchApi.getProject(kind, slug))
      setErr(null)
    } catch (e) {
      setErr(errMsg(e, 'Could not load the project'))
    }
  }, [kind, slug])

  useEffect(() => {
    void load()
  }, [load])
  // Sessions and the editor exist only for opencode projects (Req 3.5, 3.6).
  const isOpencode = kind === 'opencode'
  useEffect(() => {
    if (!isOpencode) {
      setSessions([])
      return
    }
    let live = true
    workbenchApi.listSessions(slug).then(
      (r) => { if (live) setSessions(r.sessions ?? []) },
      (e) => { if (live) setErr(errMsg(e, 'Could not load sessions')) },
    )
    return () => { live = false }
  }, [isOpencode, slug])

  // Menagerie task status, refreshed while the project is open (Req 3.11).
  const isMenagerie = kind === 'menagerie'
  const [opencodeWebUrl, setOpencodeWebUrl] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    workbenchApi
      .config()
      .then((c) => alive && setOpencodeWebUrl(c.opencode_web_url ?? null))
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [])
  const [taskStatus, setTaskStatus] = useState<WorkbenchIdeTaskStatus | null>(null)
  const [taskErr, setTaskErr] = useState<string | null>(null)
  useEffect(() => {
    if (!isMenagerie) {
      setTaskStatus(null)
      setTaskErr(null)
      return
    }
    let live = true
    const poll = () => {
      workbenchApi.ideTaskStatus(kind, slug).then(
        (s) => { if (live) { setTaskStatus(s); setTaskErr(null) } },
        (e) => { if (live) setTaskErr(errMsg(e, 'Could not load task status')) },
      )
    }
    poll()
    const t = setInterval(poll, TASK_STATUS_POLL_MS)
    return () => { live = false; clearInterval(t) }
  }, [isMenagerie, kind, slug])

  const archive = async () => {
    setBusy(true)
    try {
      await workbenchApi.archiveProject(kind, slug)
      await load()
    } catch (e) {
      setErr(errMsg(e, 'Could not archive the project'))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    setBusy(true)
    try {
      await workbenchApi.deleteProject(kind, slug, typed)
      navigate('/workbench')
    } catch (e) {
      setErr(errMsg(e, 'Could not delete the project'))
    } finally {
      setBusy(false)
    }
  }

  const handoffs = recentHandoffs(project?.handoffs)
  const title = typeof project?.title === 'string' && project.title ? project.title : slug

  return (
    <div className="space-y-4 p-4" data-testid="workbench-project-view">
      <Link to="/workbench" className="text-sm underline">Back to Workbench</Link>
      {err && <p role="alert" className="text-sm text-danger">{err}</p>}

      {project && (
        <>
          <header className="space-y-1">
            <h1 className="text-lg font-semibold">{title}</h1>
            <p className="text-xs text-secondary">
              {project.slug} · {project.kind}
              {project.archived && (
                <span className="ml-2 rounded border border-border px-1" data-testid="wb-archived-badge">archived</span>
              )}
            </p>
          </header>

          <section data-testid="wb-handoffs">
            <h2 className="mb-1 text-sm font-medium">Recent handoffs ({handoffs.length})</h2>
            {handoffs.length === 0 ? (
              <p className="text-sm text-secondary">No handoffs.</p>
            ) : (
              <ul className="space-y-1">
                {handoffs.map((h) => (
                  <li key={h.id} className="flex items-center gap-2 rounded border border-border p-2 text-sm">
                    <span>{h.title || h.id}</span>
                    {h.status && <span className="text-xs text-secondary">{h.status}</span>}
                    {h.createdAt && <span className="ml-auto text-xs text-secondary">{h.createdAt}</span>}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {isOpencode && (
            <section className="space-y-1" data-testid="wb-sessions">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-medium">Sessions ({sessions.length})</h2>
                <Link to={`/workbench/${encodeURIComponent(slug)}/edit`} className="ml-auto text-sm underline"
                  data-testid="wb-editor-link">Open editor</Link>
              </div>
              {sessions.length === 0 ? (
                <p className="text-sm text-secondary">No sessions.</p>
              ) : (
                <ul className="space-y-1">
                  {sessions.map((x) => (
                    <li key={x.id} className="flex items-center gap-2 rounded border border-border p-2 text-sm">
                      <span>{x.id}</span>
                      {x.model && <span className="text-xs text-secondary">{x.model}</span>}
                      {x.status && <span className="ml-auto text-xs text-secondary">{x.status}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}
          {isMenagerie && (
            <section className="space-y-1" data-testid="wb-task-status">
              <h2 className="text-sm font-medium">Task status</h2>
              {taskErr && <p role="alert" className="text-sm text-danger">{taskErr}</p>}
              <p className="text-sm">
                <span data-testid="wb-task-state">{taskStatus?.state || 'unknown'}</span>
                {taskStatus?.task && <span className="ml-2 text-xs text-secondary">{taskStatus.task}</span>}
                {taskStatus?.detail && <span className="ml-2 text-xs text-secondary">{taskStatus.detail}</span>}
              </p>
            </section>
          )}
          {isOpencode && opencodeWebUrl && (
            <section className="space-y-1" aria-label="opencode web" data-testid="wb-opencode-web">
              <h2 className="text-sm font-medium">opencode</h2>
              <p className="text-xs text-secondary">
                Opens opencode&apos;s own web UI. Pick the project{' '}
                <code>/projects/{slug}</code>; other directories are refused.
              </p>
              <QrLink url={opencodeWebUrl} label="Open opencode" />
            </section>
          )}
          {isMenagerie && (
            <>
              <IdeFrame kind={kind as WorkbenchKind} slug={slug} />
              <section className="space-y-1" aria-label="Open on another device">
                <QrLink
                  url={`${window.location.origin}/workbench/menagerie/${encodeURIComponent(slug)}`}
                  label="Link to this project"
                />
              </section>
            </>
          )}
          <section className="space-y-2" data-testid="wb-controls">
            <div className="flex gap-2">
              <button type="button" className={btn} disabled={busy || !!project.archived}
                onClick={() => void archive()} data-testid="wb-archive">Archive</button>
              <button type="button" className={`${btn} text-danger`} disabled={busy}
                onClick={() => setConfirming(true)} data-testid="wb-delete">Delete</button>
            </div>
            {confirming && (
              <div className="space-y-2 rounded border border-border p-3 text-xs text-secondary" data-testid="wb-delete-confirm">
                <label className="block">
                  Type <strong>{project.slug}</strong> to confirm deletion
                  <input className="mt-1 w-full rounded border border-border bg-transparent p-1 text-sm"
                    value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="wb-delete-input" />
                </label>
                <div className="flex gap-2">
                  <button type="button" className={`${btn} text-danger`}
                    disabled={busy || typed !== project.slug} onClick={() => void remove()}
                    data-testid="wb-delete-confirm-btn">Delete permanently</button>
                  <button type="button" className={btn}
                    onClick={() => { setConfirming(false); setTyped('') }}>Cancel</button>
                </div>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}
