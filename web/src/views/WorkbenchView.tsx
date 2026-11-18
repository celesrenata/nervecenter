import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  workbenchApi,
  type WorkbenchHealth,
  type WorkbenchKind,
  type WorkbenchProject,
  type WorkbenchUnregistered,
} from '../api/workbench'
import { groupProjects } from '../components/workbench/groupProjects'

/**
 * WorkbenchView: projects grouped by Workspace_Kind, create dialog, import of
 * unregistered directories, and a low-space banner (Req 2.12, 3.3, 3.4, 3.13, 9.5).
 * The archived filter lives in component state, so it resets when the Owner leaves /workbench.
 */
const LOW_SPACE_BYTES = 5_368_709_120
const HEALTH_POLL_MS = 30_000
const input = 'mt-1 w-full rounded border border-border bg-transparent p-1 text-sm'
const KINDS: WorkbenchKind[] = ['opencode', 'menagerie']

const errMsg = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)

function formatBytes(n: number): string {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function LowSpaceBanner({ health }: { health: WorkbenchHealth | null }) {
  if (!health) return null
  const low = Object.entries(health).filter(
    ([, s]) => s.free_bytes !== null && s.free_bytes !== undefined && s.free_bytes < LOW_SPACE_BYTES,
  )
  if (low.length === 0) return null
  return (
    <div role="status" data-testid="wb-low-space" className="rounded border border-warning p-2 text-sm text-warning">
      {low.map(([store, s]) => (
        <p key={store}>Low space on {store}: {formatBytes(s.free_bytes as number)} free</p>
      ))}
    </div>
  )
}

function CreateDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [kind, setKind] = useState<WorkbenchKind>('opencode')
  const [slug, setSlug] = useState('')
  const [title, setTitle] = useState('')
  const [gitUrl, setGitUrl] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    try {
      await workbenchApi.createProject({
        kind,
        slug: slug.trim(),
        title: title.trim(),
        git_url: gitUrl.trim() || undefined,
      })
      onCreated()
      onClose()
    } catch (e) {
      setErr(errMsg(e, 'Could not create the project'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div role="dialog" aria-label="Create project" data-testid="wb-create-dialog"
      className="grid grid-cols-2 gap-2 rounded border border-border p-3 text-xs text-secondary">
      <label>Kind
        <select className={input} value={kind} onChange={(e) => setKind(e.target.value as WorkbenchKind)}>
          {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
      </label>
      <label>Slug<input className={input} value={slug} onChange={(e) => setSlug(e.target.value)} /></label>
      <label className="col-span-2">Title
        <input className={input} maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className="col-span-2">Git URL (optional)
        <input className={input} value={gitUrl} onChange={(e) => setGitUrl(e.target.value)} />
      </label>
      <div className="col-span-2 flex gap-2">
        <button type="button" disabled={busy || !slug.trim() || !title.trim()} onClick={save}
          className="rounded border border-border px-3 py-1 text-sm disabled:opacity-50">Create</button>
        <button type="button" onClick={onClose} className="rounded border border-border px-3 py-1 text-sm">Cancel</button>
      </div>
      {err && <p role="alert" className="col-span-2 text-sm text-danger">{err}</p>}
    </div>
  )
}

export default function WorkbenchView() {
  const [projects, setProjects] = useState<WorkbenchProject[]>([])
  const [unregistered, setUnregistered] = useState<WorkbenchUnregistered[]>([])
  const [health, setHealth] = useState<WorkbenchHealth | null>(null)
  const [showArchived, setShowArchived] = useState(false)
  const [creating, setCreating] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [p, u] = await Promise.all([workbenchApi.listProjects(), workbenchApi.listUnregistered()])
      setProjects(p.projects)
      setUnregistered(u.unregistered)
      setErr(null)
    } catch (e) {
      setErr(errMsg(e, 'Could not load projects'))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const h = await workbenchApi.health()
        if (!cancelled) setHealth(h)
      } catch {
        /* keep the last known health; the project list reports its own errors */
      }
    }
    void poll()
    const id = setInterval(poll, HEALTH_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const groups = useMemo(() => groupProjects(projects, showArchived), [projects, showArchived])

  const doImport = async (u: WorkbenchUnregistered) => {
    try {
      await workbenchApi.importProject({ kind: u.kind, name: u.name })
      await load()
    } catch (e) {
      setErr(errMsg(e, 'Could not import the directory'))
    }
  }

  return (
    <div className="space-y-4 p-4" data-testid="workbench-view">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Workbench</h1>
        <div className="flex items-center gap-3 text-sm">
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)}
              data-testid="wb-show-archived" />
            Show archived
          </label>
          <button type="button" onClick={() => setCreating(true)}
            className="rounded border border-border px-3 py-1">New project</button>
        </div>
      </div>

      <LowSpaceBanner health={health} />
      {err && <p role="alert" className="text-sm text-danger">{err}</p>}
      {creating && <CreateDialog onClose={() => setCreating(false)} onCreated={() => void load()} />}

      {groups.length === 0 && !err && <p className="text-sm text-secondary">No projects.</p>}
      {groups.map((g) => (
        <section key={g.kind} data-testid={`wb-group-${g.kind}`}>
          <h2 className="mb-1 text-sm font-medium">{g.kind} ({g.count})</h2>
          <ul className="space-y-1">
            {g.projects.map((p) => (
              <li key={`${p.kind}/${p.slug}`} className="flex items-center gap-2 rounded border border-border p-2 text-sm">
                <Link to={`/workbench/${encodeURIComponent(p.kind)}/${encodeURIComponent(p.slug)}`} className="underline">
                  {typeof p.title === 'string' && p.title ? p.title : p.slug}
                </Link>
                <span className="text-xs text-secondary">{p.slug}</span>
                {p.archived && (
                  <span className="rounded border border-border px-1 text-xs text-secondary" data-testid="wb-archived-badge">
                    archived
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}

      {unregistered.length > 0 && (
        <section data-testid="wb-unregistered">
          <h2 className="mb-1 text-sm font-medium">Unregistered directories ({unregistered.length})</h2>
          <ul className="space-y-1">
            {unregistered.map((u) => (
              <li key={`${u.kind}/${u.name}`} className="flex items-center gap-2 rounded border border-border p-2 text-sm">
                <span>{u.name}</span>
                <span className="text-xs text-secondary">{u.kind}</span>
                <button type="button" onClick={() => void doImport(u)}
                  className="ml-auto rounded border border-border px-2 py-0.5 text-xs">Import</button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
