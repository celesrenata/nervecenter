import { useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { resourcesApi, type Resource } from '../api/resources'

/** Multi-select of the normal resources an agent may read (NS-ADR-125). */
export function AgentResourceLinks({ agentId }: { agentId: string }) {
  const [all, setAll] = useState<Resource[]>([])
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    Promise.all([resourcesApi.list(), resourcesApi.agentLinks(agentId)])
      .then(([rows, linked]) => {
        if (!live) return
        setAll(rows.filter((r) => r.sensitivity === 'normal'))
        setChosen(new Set(linked.map((r) => r.id)))
      })
      .catch(() => live && setMsg('Could not load resources.'))
    return () => {
      live = false
    }
  }, [agentId])

  const toggle = (id: string) =>
    setChosen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const save = async () => {
    setBusy(true)
    setMsg(null)
    try {
      await resourcesApi.setAgentLinks(agentId, [...chosen])
      setMsg('Linked resources saved.')
    } catch (e) {
      setMsg(e instanceof ApiError ? `${e.status} — ${e.detail}` : 'Request failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <fieldset data-testid="agent-edit-resources" className="space-y-2">
      <legend className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">
        Linked resources (the agent can read these as text)
      </legend>
      {all.length === 0 ? (
        <p className="text-sm text-muted">No linkable resources. Upload one on the Resources page.</p>
      ) : (
        all.map((r) => (
          <label key={r.id} className="flex items-center gap-2 text-sm text-primary">
            <input
              type="checkbox"
              data-testid={`agent-resource-${r.id}`}
              checked={chosen.has(r.id)}
              onChange={() => toggle(r.id)}
            />
            {r.name}
          </label>
        ))
      )}
      <div className="flex items-center gap-3">
        <button
          type="button"
          data-testid="agent-resources-save"
          disabled={busy}
          onClick={() => void save()}
          className="rounded border border-line px-3 py-1.5 text-sm hover:bg-hover disabled:opacity-50"
        >
          Save linked resources
        </button>
        {msg && <span role="status" data-testid="agent-resources-msg" className="text-sm text-muted">{msg}</span>}
      </div>
    </fieldset>
  )
}
