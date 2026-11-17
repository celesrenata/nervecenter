import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import { resourcesApi, type Resource, type ResourceSensitivity } from '../api/resources'

/**
 * Resources (NS-ADR-125): files you upload are stored encrypted on the object
 * store. "Normal" files can be linked to agents (Agents > Edit) which read them
 * as text; "Secret" files stay with you and the backend and are never shown to agents.
 */
const errText = (e: unknown) => (e instanceof ApiError ? `${e.status} — ${e.detail}` : 'Request failed')
const btn = 'rounded px-3 py-1.5 text-sm font-medium disabled:opacity-50'
const primary = `${btn} bg-blue-600 text-white hover:bg-blue-700`
const secondary = `${btn} border border-line text-primary hover:bg-hover`

const size = (n: number) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`)

export function ResourcesView() {
  const [rows, setRows] = useState<Resource[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [sensitivity, setSensitivity] = useState<ResourceSensitivity>('normal')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setRows(await resourcesApi.list())
      setError(null)
    } catch (e) {
      setError(errText(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const upload = async (e: FormEvent) => {
    e.preventDefault()
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      await resourcesApi.upload(file, sensitivity)
      setFile(null)
      ;(e.target as HTMLFormElement).reset()
      await refresh()
    } catch (err) {
      setError(errText(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (r: Resource) => {
    if (!window.confirm(`Delete "${r.name}"? It is removed from storage and from every agent it is linked to.`)) return
    try {
      await resourcesApi.remove(r.id)
      await refresh()
    } catch (err) {
      setError(errText(err))
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold text-primary">Resources</h1>
      <p className="text-sm text-muted">
        Upload files (up to 20 MB). They are stored encrypted. Link a normal file to an agent from the agent&apos;s Edit
        panel; secret files are never visible to agents.
      </p>
      <form onSubmit={upload} data-testid="resource-upload" className="flex flex-wrap items-end gap-3 rounded-lg border border-line p-4">
        <label className="text-sm">
          <span className="block text-muted">File</span>
          <input data-testid="resource-file" type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <label className="text-sm">
          <span className="block text-muted">Sensitivity</span>
          <select
            data-testid="resource-sensitivity"
            className="rounded border border-line bg-surface px-2 py-1.5 text-sm"
            value={sensitivity}
            onChange={(e) => setSensitivity(e.target.value as ResourceSensitivity)}
          >
            <option value="normal">Normal (can be linked to agents)</option>
            <option value="secret">Secret (owner and backend only)</option>
          </select>
        </label>
        <button type="submit" data-testid="resource-submit" className={primary} disabled={!file || busy}>
          {busy ? 'Uploading…' : 'Upload'}
        </button>
      </form>
      {error && (
        <div role="alert" data-testid="resources-error" className="rounded-md bg-red-100 p-3 text-sm font-medium text-red-800 dark:bg-red-900/40 dark:text-red-300">
          {error}
        </div>
      )}
      {loading ? (
        <p className="text-muted">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-muted" data-testid="resources-empty">No resources yet.</p>
      ) : (
        <ul className="space-y-2" data-testid="resources-list">
          {rows.map((r) => (
            <li key={r.id} data-testid={`resource-${r.id}`} className="flex flex-wrap items-center gap-3 rounded-lg border border-line p-3">
              <span className="font-medium text-primary">{r.name}</span>
              <span
                data-testid={`resource-badge-${r.id}`}
                className={`rounded px-2 py-0.5 text-xs font-semibold ${r.sensitivity === 'secret' ? 'bg-red-100 text-red-800' : 'bg-green-100 text-green-800'}`}
              >
                {r.sensitivity}
              </span>
              <span className="text-xs text-muted">{size(r.size)} · {r.content_type}</span>
              <span className="ml-auto flex gap-2">
                <button type="button" className={secondary} onClick={() => void resourcesApi.download(r).catch((e) => setError(errText(e)))}>
                  Download
                </button>
                <button type="button" data-testid={`resource-delete-${r.id}`} className={secondary} onClick={() => void remove(r)}>
                  Delete
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
