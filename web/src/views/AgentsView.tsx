import { useState, useEffect, type FormEvent } from 'react'
import { agentsApi, type AgentUpdatePayload } from '../api/agents'
import { inferenceApi, type AvailableInference } from '../api/inference'
import {
  durableInferenceApi,
  type DurableCombo,
} from '../api/inferenceDurable'
import { apiFetch, apiErrorFromResponse, ApiError } from '../api/client'

/**
 * Agents view — round 3 (AG-2, AG-3 resolved).
 *
 * AG-2: every status carries visible text AND an `aria-label` — never a bare
 * dot.
 * AG-3 (round 3): the backend now exposes a real agent *create* route
 * (`POST /api/agents`, backend/api/endpoints/agents.py), so '+ Add Agent' is
 * enabled and opens a create form. The form submits to the write route; a 201
 * refreshes the list so the new instance renders, and any non-2xx (e.g. a 409
 * name conflict) is surfaced verbatim rather than faked as success.
 * Persisted agent instances have no live telemetry, so those fields render as
 * "none" / omitted rather than invented numbers.
 */

interface Agent {
  id: string
  name: string
  type: string
  status: string
  description: string | null
  prompt_template: string | null
  tools: string[]
  capabilities: Record<string, unknown> | null
  lastHeartbeat: string | null
  cpuUsage: number | null
  memoryUsage: number | null
  tasksCompleted: number | null
  tasksFailed: number | null
  /** Inference binding (`null`/absent = use service default). */
  inference?: { provider?: string | null; model?: string | null } | null
  /** Durable combo binding (`null`/absent = no combo; shadows per-agent inference). */
  combo_id?: string | null
}

// The 14 native agent types (spec §26), from backend/agents/registry.py.
// Used as the options for the create form's type <select>.
const NATIVE_AGENT_TYPES = [
  'planner',
  'repository_inspector',
  'runtime_inspector',
  'log_investigator',
  'infrastructure_inspector',
  'architecture_analyst',
  'incident_correlator',
  'code_reviewer',
  'test_runner',
  'benchmark_agent',
  'research_agent',
  'verifier',
  'context_critic',
  'route_critic',
] as const

const AgentsView = () => {
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)
  const [fetchError, setFetchError] = useState<{ status: number; detail: string } | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [agentName, setAgentName] = useState('')
  const [agentType, setAgentType] = useState<string>(NATIVE_AGENT_TYPES[0])
  const [agentDescription, setAgentDescription] = useState('')
  const [formBusy, setFormBusy] = useState(false)
  const [formError, setFormError] = useState<{ status: number; detail: string } | null>(null)
  const [formSuccess, setFormSuccess] = useState(false)

  // Detail / edit for *persisted* agent instances (native types are read-only
  // manifests and carry no edit affordance).
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null)
  const [editAgentName, setEditAgentName] = useState('')
  const [editAgentDescription, setEditAgentDescription] = useState('')
  const [editAgentPrompt, setEditAgentPrompt] = useState('')
  const [editAgentTools, setEditAgentTools] = useState('')
  const [editBusy, setEditBusy] = useState(false)
  const [editLoading, setEditLoading] = useState(false)
  const [editError, setEditError] = useState<{ status: number; detail: string } | null>(null)
  const [editSuccess, setEditSuccess] = useState(false)

  // Per-agent inference control (INFER_CONTROL_CONTRACT.md §3 + web scope): a
  // persisted agent can bind a {provider, model}. The editor is opened per-row
  // (not on mount) so no extra requests fire during the list render. Native
  // types are process-only manifests — they show a read-only "native default"
  // badge and get no editor (honested: there is no write route for them here).
  const [inferenceAgentId, setInferenceAgentId] = useState<string | null>(null)
  const [inferProvider, setInferProvider] = useState('')
  const [inferModel, setInferModel] = useState('')
  const [inferBusy, setInferBusy] = useState(false)
  const [inferError, setInferError] = useState<{ status: number; detail: string } | null>(null)
  const [inferSuccess, setInferSuccess] = useState(false)
  const [inferOptions, setInferOptions] = useState<AvailableInference | null>(null)
  const [inferOptionsLoading, setInferOptionsLoading] = useState(false)
  // Durable combo binding (providers feature): a combo shadows the per-agent
  // provider/model. Loaded lazily with the rest of the inference editor.
  const [inferCombo, setInferCombo] = useState('')
  const [inferCombos, setInferCombos] = useState<DurableCombo[]>([])
  const [inferCombosLoading, setInferCombosLoading] = useState(false)

  const openInference = async (agentId: string) => {
    setInferenceAgentId(agentId)
    setInferError(null)
    setInferSuccess(false)
    const agent = agents.find((a) => a.id === agentId)
    setInferProvider(agent?.inference?.provider ?? '')
    setInferModel(agent?.inference?.model ?? '')
    setInferCombo(agent?.combo_id ?? '')
    if (inferOptions === null && !inferOptionsLoading) {
      setInferOptionsLoading(true)
      try {
        setInferOptions(await inferenceApi.getAvailable())
      } catch {
        // Honest: empty options; the fields still accept manual values.
        setInferOptions({ providers: [], models: [], default: { provider: 'omniroute', model: 'nerve-ask' } })
      } finally {
        setInferOptionsLoading(false)
      }
    }
    if (inferCombos.length === 0 && !inferCombosLoading) {
      setInferCombosLoading(true)
      try {
        setInferCombos(await durableInferenceApi.listCombos())
      } catch {
        // Honest: no combos surfaced; the picker just shows the default.
        setInferCombos([])
      } finally {
        setInferCombosLoading(false)
      }
    }
  }

  const closeInference = () => {
    setInferenceAgentId(null)
    setInferProvider('')
    setInferModel('')
    setInferCombo('')
    setInferError(null)
    setInferSuccess(false)
    setInferBusy(false)
  }

  const submitInference = async () => {
    if (!inferenceAgentId) return
    setInferBusy(true)
    setInferError(null)
    setInferSuccess(false)
    try {
      // PATCH /api/agents/{id}: partial update carrying the binding(s).
      // A null/empty binding clears to the service default. A non-empty combo
      // shadows the per-agent inference (resolved at ask time).
      const payload: AgentUpdatePayload = {
        inference:
          inferProvider || inferModel
            ? {
                provider: inferProvider || null,
                model: inferModel || null,
              }
            : null,
        combo_id: inferCombo,
      }
      await agentsApi.update(inferenceAgentId, payload)
      setInferSuccess(true)
      const list = await agentsApi.list()
      setAgents(list.data as Agent[])
      setFetchError(null)
    } catch (error) {
      if (error instanceof ApiError) {
        setInferError({ status: error.status, detail: error.detail })
      } else {
        setInferError({ status: 0, detail: 'Request failed (network error)' })
      }
    } finally {
      setInferBusy(false)
    }
  }

  useEffect(() => {
    const fetchAgents = async () => {
      try {
        const response = await agentsApi.list()
        setAgents(response.data as Agent[])
        setFetchError(null)
      } catch (error) {
        if (error instanceof ApiError) {
          setFetchError({ status: error.status, detail: error.detail })
        } else {
          setFetchError({ status: 0, detail: 'Request failed (network error)' })
        }
      } finally {
        setLoading(false)
      }
    }

    fetchAgents()
  }, [])

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'active':
        return 'bg-green-500'
      case 'inactive':
        return 'bg-gray-500'
      case 'error':
        return 'bg-red-500'
      case 'paused':
        return 'bg-yellow-500'
      default:
        return 'bg-gray-500'
    }
  }

  const getProgressColor = (value: number) => {
    if (value < 50) return 'bg-green-500'
    if (value < 80) return 'bg-yellow-500'
    return 'bg-red-500'
  }

  const openForm = () => {
    setShowForm(true)
    setFormError(null)
    setFormSuccess(false)
  }

  const closeForm = () => {
    setShowForm(false)
    setAgentName('')
    setAgentType(NATIVE_AGENT_TYPES[0])
    setAgentDescription('')
    setFormError(null)
    setFormSuccess(false)
    setFormBusy(false)
  }

  const submitAgent = async (e: FormEvent) => {
    e.preventDefault()
    if (!agentName.trim()) {
      setFormError({ status: 0, detail: 'Name is required.' })
      return
    }
    setFormBusy(true)
    setFormError(null)
    setFormSuccess(false)
    try {
      // AG-3 (round 3): create a durable agent instance. 201 -> refresh the
      // list so the new row renders; non-2xx (e.g. 409 duplicate name) is
      // surfaced verbatim, not faked as success.
      const res = await apiFetch('/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: agentName.trim(),
          type: agentType,
          description: agentDescription.trim() || null,
        }),
      })
      if (!res.ok) {
        throw await apiErrorFromResponse(res)
      }
      setFormSuccess(true)
      const response = await agentsApi.list()
      setAgents(response.data as Agent[])
      setFetchError(null)
    } catch (error) {
      if (error instanceof ApiError) {
        setFormError({ status: error.status, detail: error.detail })
      } else {
        setFormError({ status: 0, detail: 'Request failed (network error)' })
      }
    } finally {
      setFormBusy(false)
    }
  }

  // Only persisted instances (ids that are not native agent-type strings) are
  // editable; native types are read-only manifests.
  const isPersisted = (id: string) => !(NATIVE_AGENT_TYPES as readonly string[]).includes(id)

  const openEdit = async (agentId: string) => {
    setEditingAgentId(agentId)
    setEditLoading(true)
    setEditError(null)
    setEditSuccess(false)
    try {
      // Detail fetch (GET /agents/{id}) now resolves persisted instances too.
      const res = await agentsApi.get(agentId)
      const agent = res.data
      setEditAgentName(agent.name)
      setEditAgentDescription(agent.description ?? '')
      setEditAgentPrompt(agent.prompt_template ?? '')
      setEditAgentTools((agent.tools ?? []).join(', '))
    } catch (error) {
      if (error instanceof ApiError) {
        setEditError({ status: error.status, detail: error.detail })
      } else {
        setEditError({ status: 0, detail: 'Request failed (network error)' })
      }
    } finally {
      setEditLoading(false)
    }
  }

  const closeEdit = () => {
    setEditingAgentId(null)
    setEditAgentName('')
    setEditAgentDescription('')
    setEditAgentPrompt('')
    setEditAgentTools('')
    setEditError(null)
    setEditSuccess(false)
    setEditBusy(false)
  }

  const submitEdit = async (e: FormEvent) => {
    e.preventDefault()
    if (!editingAgentId) return
    if (!editAgentName.trim()) {
      setEditError({ status: 0, detail: 'Name is required.' })
      return
    }
    setEditBusy(true)
    setEditError(null)
    setEditSuccess(false)
    try {
      // PATCH /api/agents/{id}: partial update. A 409 (duplicate name) is
      // surfaced verbatim, not faked as success.
      const payload: AgentUpdatePayload = {
        name: editAgentName.trim(),
        description: editAgentDescription.trim() || null,
        prompt_template: editAgentPrompt.trim() || null,
        tools: editAgentTools
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
      }
      await agentsApi.update(editingAgentId, payload)
      setEditSuccess(true)
      // Refresh the list so the edited row renders its updated values.
      const list = await agentsApi.list()
      setAgents(list.data as Agent[])
      setFetchError(null)
    } catch (error) {
      if (error instanceof ApiError) {
        // 409 name conflict / 503 no-DB are surfaced verbatim.
        setEditError({ status: error.status, detail: error.detail })
      } else {
        setEditError({ status: 0, detail: 'Request failed (network error)' })
      }
    } finally {
      setEditBusy(false)
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold text-primary">
        Agents
      </h1>

      {fetchError && (
        <div
          role="alert"
          data-testid="agents-error"
          className="p-3 rounded-md bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 text-sm font-medium"
        >
          Failed to load agents: {fetchError.status} — {fetchError.detail}
        </div>
      )}

      <div className="flex justify-between items-center">
        <div className="text-muted">
          {agents.length} total agents
        </div>
        {/* AG-3 (round 3): create route exists → enabled. */}
        <button
          onClick={openForm}
          className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-lg"
        >
          + Add Agent
        </button>
      </div>

      {showForm && (
        <form
          data-testid="agent-form"
          onSubmit={submitAgent}
          className="bg-elevated p-6 rounded-lg shadow-lg space-y-4"
        >
          <h3 className="text-xl font-semibold text-primary">
            Add agent
          </h3>
          <p className="text-sm text-muted">
            Creates a durable agent instance via the write route (POST{' '}
            <code className="text-xs">/api/agents</code>). Non-2xx responses
            are shown as-is.
          </p>

          <div>
            <label
              htmlFor="agent-name"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Name
            </label>
            <input
              id="agent-name"
              data-testid="agent-form-name"
              type="text"
              required
              value={agentName}
              onChange={(e) => setAgentName(e.target.value)}
              placeholder="e.g. scout-1"
              className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
            />
          </div>

          <div>
            <label
              htmlFor="agent-type"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Type
            </label>
            <select
              id="agent-type"
              data-testid="agent-form-type"
              value={agentType}
              onChange={(e) => setAgentType(e.target.value)}
              className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
            >
              {NATIVE_AGENT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label
              htmlFor="agent-description"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Description
            </label>
            <textarea
              id="agent-description"
              data-testid="agent-form-description"
              rows={2}
              value={agentDescription}
              onChange={(e) => setAgentDescription(e.target.value)}
              placeholder="What does this agent do?"
              className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
            />
          </div>

          {formError && (
            <div
              role="alert"
              data-testid="agent-form-error"
              className="p-3 rounded-md bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 text-sm font-medium"
            >
              {formError.status ? `${formError.status} — ` : ''}
              {formError.detail}
            </div>
          )}
          {formSuccess && (
            <div
              role="status"
              data-testid="agent-form-success"
              className="p-3 rounded-md bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-300 text-sm font-medium"
            >
              Agent created.
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              type="submit"
              data-testid="agent-form-submit"
              disabled={formBusy}
              className="bg-blue-500 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded-lg"
            >
              {formBusy ? 'Creating…' : 'Create agent'}
            </button>
            <button
              type="button"
              onClick={closeForm}
              className="text-muted px-4 py-2"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {editingAgentId && (
        <form
          data-testid="agent-edit"
          onSubmit={submitEdit}
          className="bg-elevated p-6 rounded-lg shadow-lg space-y-4"
        >
          <h3 className="text-xl font-semibold text-primary">
            Edit agent
          </h3>
          <p className="text-sm text-muted">
            Updates a durable agent instance via the write route (PATCH{' '}
            <code className="text-xs">/api/agents/{'{'}</code>
            <code className="text-xs">id</code>
            <code className="text-xs">{'}'}</code>). A 409 (duplicate name) is
            shown verbatim.
          </p>

          {editLoading && (
            <div className="text-sm text-muted">Loading agent…</div>
          )}

          {!editLoading && (
            <>
              <div>
                <label
                  htmlFor="agent-edit-name"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Name
                </label>
                <input
                  id="agent-edit-name"
                  data-testid="agent-edit-name"
                  type="text"
                  required
                  value={editAgentName}
                  onChange={(e) => setEditAgentName(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                />
              </div>

              <div>
                <label
                  htmlFor="agent-edit-description"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Description
                </label>
                <input
                  id="agent-edit-description"
                  data-testid="agent-edit-description"
                  type="text"
                  value={editAgentDescription}
                  onChange={(e) => setEditAgentDescription(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                />
              </div>

              <div>
                <label
                  htmlFor="agent-edit-prompt"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Prompt template
                </label>
                <textarea
                  id="agent-edit-prompt"
                  data-testid="agent-edit-prompt"
                  rows={3}
                  value={editAgentPrompt}
                  onChange={(e) => setEditAgentPrompt(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                />
              </div>

              <div>
                <label
                  htmlFor="agent-edit-tools"
                  className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
                >
                  Tools (comma-separated)
                </label>
                <input
                  id="agent-edit-tools"
                  data-testid="agent-edit-tools"
                  type="text"
                  value={editAgentTools}
                  onChange={(e) => setEditAgentTools(e.target.value)}
                  placeholder="e.g. read_file, search"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                />
              </div>

              {editError && (
                <div
                  role="alert"
                  data-testid="agent-edit-error"
                  className="p-3 rounded-md bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 text-sm font-medium"
                >
                  {editError.status ? `${editError.status} — ` : ''}
                  {editError.detail}
                </div>
              )}
              {editSuccess && (
                <div
                  role="status"
                  data-testid="agent-edit-success"
                  className="p-3 rounded-md bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-300 text-sm font-medium"
                >
                  Agent updated.
                </div>
              )}
            </>
          )}

          <div className="flex items-center gap-3">
            <button
              type="submit"
              data-testid="agent-edit-save"
              disabled={editBusy || editLoading}
              className="bg-blue-500 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded-lg"
            >
              {editBusy ? 'Saving…' : 'Save changes'}
            </button>
            <button
              type="button"
              onClick={closeEdit}
              className="text-muted px-4 py-2"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* Per-agent inference binding editor (persisted agents only). */}
      {inferenceAgentId && (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void submitInference()
          }}
          className="bg-elevated p-6 rounded-lg shadow-lg mb-6"
          aria-label={`Inference binding for agent ${inferenceAgentId}`}
        >
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-primary">
              Inference binding
            </h2>
            <p className="text-xs text-muted">
              Leave both blank to use the service default.
            </p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div>
              <label
                htmlFor="agent-infer-combo"
                className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
              >
                Combo
              </label>
              <select
                id="agent-infer-combo"
                data-testid="agent-infer-combo"
                value={inferCombo}
                onChange={(e) => setInferCombo(e.target.value)}
                disabled={inferBusy}
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              >
                <option value="">None (per-agent default)</option>
                {(inferCombos.length === 0 && inferCombosLoading) && (
                  <option value="">Loading…</option>
                )}
                {inferCombos.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} ({c.provider_name} / {c.model_id})
                  </option>
                ))}
              </select>
              <p className="mt-1 text-xs text-muted">
                A combo shadows the provider/model below and is resolved at
                ask time; re-pointing it re-binds this agent with no write.
              </p>
            </div>
            <div>
              <label
                htmlFor="agent-infer-provider"
                className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
              >
                Provider
              </label>
              <select
                id="agent-infer-provider"
                data-testid="agent-infer-provider"
                value={inferProvider}
                onChange={(e) => setInferProvider(e.target.value)}
                disabled={inferBusy}
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              >
                <option value="">Default</option>
                {(inferOptions?.providers ?? []).map((p) => (
                  <option key={p.provider} value={p.provider}>
                    {p.provider}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label
                htmlFor="agent-infer-model"
                className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
              >
                Model
              </label>
              <select
                id="agent-infer-model"
                data-testid="agent-infer-model"
                value={inferModel}
                onChange={(e) => setInferModel(e.target.value)}
                disabled={inferBusy}
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              >
                <option value="">Default</option>
                {(inferOptions?.models ?? []).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div
            data-testid="agent-infer-error"
            aria-live="polite"
            className="mt-4"
          >
            {inferError && (
              <div
                className={`px-3 py-2 rounded-lg text-sm ${
                  inferError.status === 409
                    ? 'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200'
                    : 'bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200'
                }`}
              >
                <span className="font-semibold">
                  {inferError.status === 0 ? 'Error' : `HTTP ${inferError.status}`}
                </span>
                {' — '}
                {inferError.detail}
              </div>
            )}
            {inferSuccess && (
              <div
                data-testid="agent-infer-success"
                className="px-3 py-2 rounded-lg text-sm bg-green-100 text-green-900 dark:bg-green-900/40 dark:text-green-200"
              >
                Inference binding updated.
              </div>
            )}
          </div>

          <div className="flex items-center gap-3 mt-4">
            <button
              type="submit"
              data-testid="agent-infer-save"
              disabled={inferBusy}
              className="bg-blue-500 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded-lg"
            >
              {inferBusy ? 'Saving…' : 'Save binding'}
            </button>
            <button
              type="button"
              onClick={closeInference}
              className="text-muted px-4 py-2"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <div className="text-center py-12">Loading...</div>
      ) : !fetchError && agents.length === 0 ? (
        <div
          data-testid="agents-empty"
          className="bg-elevated p-8 rounded-lg shadow-lg text-center"
        >
          <p className="text-gray-700 dark:text-gray-300 font-medium">
            No agents registered.
          </p>
          <p className="text-sm text-muted mt-2">
            Agents appear here once the AgentRegistry reports them, or use
            “+ Add Agent” to create a durable agent instance via
            POST /api/agents.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {agents.map((agent) => (
            <div
              key={agent.id}
              className="bg-elevated p-6 rounded-lg shadow-lg"
            >
              <div className="flex items-start justify-between mb-4">
                <div>
                  <h3 className="text-xl font-semibold text-primary">
                    {agent.name}
                  </h3>
                  <p className="text-sm text-muted">
                    {agent.type}
                  </p>
                </div>
                {/* AG-2: text + aria-label, never a bare dot. */}
                <div className="flex items-center gap-3">
                  {isPersisted(agent.id) && (
                    <>
                      <button
                        type="button"
                        data-testid="agent-edit"
                        onClick={() => openEdit(agent.id)}
                        className="text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        Detail / Edit
                      </button>
                      {/* Inference binding (INFER_CONTROL_CONTRACT.md §3): the
                          current binding shown as a badge, opened per-row. */}
                      <span
                        data-testid="agent-inference-badge"
                        aria-label={`Agent ${agent.name} inference: ${
                          agent.inference?.model ?? agent.inference?.provider ?? 'default'
                        }`}
                        className="text-xs font-mono text-gray-600 dark:text-gray-400"
                      >
                        {agent.inference?.model ?? agent.inference?.provider ?? 'default'}
                      </span>
                      {/* Durable combo binding (providers feature): a bound
                          combo shadows the per-agent inference at ask time. */}
                      {agent.combo_id && (
                        <span
                          data-testid="agent-combo-badge"
                          aria-label={`Agent ${agent.name} combo binding: ${agent.combo_id}`}
                          className="text-xs font-mono px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
                        >
                          combo:{agent.combo_id}
                        </span>
                      )}
                      <button
                        type="button"
                        data-testid="agent-inference"
                        onClick={() => openInference(agent.id)}
                        className="text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        Inference
                      </button>
                    </>
                  )}
                  <div
                    role="status"
                    aria-label={`Agent ${agent.name} status: ${agent.status}`}
                    data-testid="agent-status"
                    className="flex items-center gap-2"
                  >
                    <span
                      aria-hidden="true"
                      className={`w-3 h-3 rounded-full ${getStatusColor(
                        agent.status
                      )}`}
                    />
                    <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                      {agent.status}
                    </span>
                  </div>
                </div>
              </div>
              <div className="space-y-3">
                {agent.cpuUsage != null && agent.memoryUsage != null ? (
                  <>
                    <div>
                      <div className="flex justify-between text-sm mb-1">
                        <span className="text-muted">
                          CPU Usage
                        </span>
                        <span className="text-primary">
                          {agent.cpuUsage}%
                        </span>
                      </div>
                      <div className="w-full h-2 bg-gray-200 dark:bg-gray-700 rounded-full">
                        <div
                          className={`h-2 rounded-full ${getProgressColor(
                            agent.cpuUsage
                          )}`}
                          style={{ width: `${agent.cpuUsage}%` }}
                        />
                      </div>
                    </div>
                    <div>
                      <div className="flex justify-between text-sm mb-1">
                        <span className="text-muted">
                          Memory Usage
                        </span>
                        <span className="text-primary">
                          {agent.memoryUsage}%
                        </span>
                      </div>
                      <div className="w-full h-2 bg-gray-200 dark:bg-gray-700 rounded-full">
                        <div
                          className={`h-2 rounded-full ${getProgressColor(
                            agent.memoryUsage
                          )}`}
                          style={{ width: `${agent.memoryUsage}%` }}
                        />
                      </div>
                    </div>
                  </>
                ) : (
                  <div className="text-sm text-gray-400 dark:text-gray-500">
                    No live telemetry reported.
                  </div>
                )}
                <div className="flex justify-between text-sm">
                  <span className="text-muted">
                    Tasks completed
                  </span>
                  <span className="text-primary">
                    {agent.tasksCompleted ?? '—'}
                  </span>
                </div>
                <div className="flex justify-between text-sm">
                  <span className="text-muted">
                    Tasks failed
                  </span>
                  <span className="text-primary">
                    {agent.tasksFailed ?? '—'}
                  </span>
                </div>
                <div className="text-xs text-gray-400 dark:text-gray-500">
                  Last heartbeat: {agent.lastHeartbeat ?? 'none'}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export { AgentsView }
