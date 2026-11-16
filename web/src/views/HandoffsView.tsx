import { useCallback, useEffect, useState } from 'react'
import { handoffsApi, type Handoff, type HandoffRunnerStatus } from '../api/handoffs'

/**
 * HandoffsView (NS-ADR-106): coding handoffs queued for central opencode (in the
 * cluster) or the Mac (Swift, apple/, Menagerie). Shows the queue and results.
 * Runs never push to a git remote unless you approve push for that handoff.
 */
const input = 'mt-1 w-full rounded border border-border bg-transparent p-1 text-sm'
const RUNNER_LABEL: Record<string, string> = { central: 'Central opencode', mac: 'Mac' }

function NewHandoff({ onCreated }: { onCreated: () => void }) {
  const [target, setTarget] = useState('opencode')
  const [title, setTitle] = useState('')
  const [repo, setRepo] = useState('')
  const [brief, setBrief] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const save = async () => {
    try {
      await handoffsApi.create({ target, title: title.trim(), brief: brief.trim(), repo: repo.trim() || undefined })
      setTitle('')
      setBrief('')
      setRepo('')
      setErr(null)
      onCreated()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not queue the handoff')
    }
  }
  return (
    <div className="grid grid-cols-2 gap-2 text-xs text-secondary" data-testid="ho-new">
      <label>Tool
        <select className={input} value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="opencode">opencode</option>
          <option value="menagerie">Menagerie (Mac)</option>
        </select>
      </label>
      <label>Repository<input className={input} value={repo} onChange={(e) => setRepo(e.target.value)} /></label>
      <label className="col-span-2">Title<input className={input} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="col-span-2">Brief<textarea className={input} rows={3} value={brief} onChange={(e) => setBrief(e.target.value)} /></label>
      <button type="button" disabled={!title.trim() || !brief.trim()} onClick={save}
        className="col-span-2 rounded border border-border px-3 py-1 text-sm disabled:opacity-50">Queue handoff</button>
      {err && <p role="alert" className="col-span-2 text-sm text-danger">{err}</p>}
    </div>
  )
}

function Row({ h, onChanged }: { h: Handoff; onChanged: () => void }) {
  const [err, setErr] = useState<string | null>(null)
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn()
      setErr(null)
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Action failed')
    }
  }
  return (
    <li className="rounded border border-border p-2" data-testid={`ho-${h.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold text-primary">{h.title}</span>
        <span className="text-xs">{h.target} on {RUNNER_LABEL[h.runner]}{h.repo ? ` (${h.repo})` : ''}</span>
        <span className="text-xs" data-testid="ho-status">{h.status}</span>
        {h.push_approved && <span className="text-xs text-warning">push approved</span>}
        <span className="text-xs text-muted">by {h.requested_by}</span>
      </div>
      <details className="text-xs"><summary>Brief</summary><pre className="whitespace-pre-wrap">{h.brief}</pre></details>
      {h.result_summary && <pre className="mt-1 whitespace-pre-wrap text-sm" data-testid="ho-result">{h.result_summary}</pre>}
      {h.error && <p className="text-xs text-danger">Error: {h.error}</p>}
      <div className="mt-1 flex gap-2 text-xs">
        {h.status === 'queued' && !h.push_approved && (
          <button type="button" className="rounded border border-border px-2" onClick={() => void act(() => handoffsApi.approvePush(h.id))}>Approve push</button>
        )}
        {h.status === 'queued' && (
          <button type="button" className="rounded border border-border px-2" onClick={() => void act(() => handoffsApi.cancel(h.id))}>Cancel</button>
        )}
        {(h.status === 'failed' || h.status === 'cancelled' || h.status === 'claimed') && (
          <button type="button" className="rounded border border-border px-2" onClick={() => void act(() => handoffsApi.retry(h.id))}>Requeue</button>
        )}
      </div>
      {err && <p role="alert" className="text-xs text-danger">{err}</p>}
    </li>
  )
}

export default function HandoffsView() {
  const [items, setItems] = useState<Handoff[]>([])
  const [status, setStatus] = useState<HandoffRunnerStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setItems(await handoffsApi.list())
      setStatus(await handoffsApi.status().catch(() => null))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load handoffs')
    }
  }, [])

  useEffect(() => {
    void load()
    const t = setInterval(() => void load(), 15000)
    return () => clearInterval(t)
  }, [load])

  return (
    <div className="space-y-4 p-4">
      <h1 className="text-lg font-semibold text-primary">Coding handoffs</h1>
      <p className="text-xs text-muted">
        Work for central opencode (Linux repos) or your Mac (Swift, apple/, Menagerie). NerveCenter runs no code
        itself. Nothing is pushed to a git remote unless you approve push for that handoff.
      </p>
      {status && (
        <p className="text-xs text-secondary" data-testid="ho-runners">
          Central opencode: {status.central.configured ? (status.central.busy ? 'working' : 'ready') : 'not configured'}.
          Mac: {status.mac.pull_path_enabled ? 'connected' : 'pull route not enabled yet'}.
        </p>
      )}
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <section className="rounded border border-border p-4">
        <h2 className="text-sm font-semibold text-primary">Queue and results</h2>
        {items.length === 0 && <p className="text-xs text-muted">No handoffs yet.</p>}
        <ul className="mt-2 space-y-2 text-sm text-secondary">
          {items.map((h) => <Row key={h.id} h={h} onChanged={load} />)}
        </ul>
      </section>
      <section className="rounded border border-border p-4">
        <h2 className="text-sm font-semibold text-primary">New handoff</h2>
        <NewHandoff onCreated={load} />
      </section>
    </div>
  )
}
