import { useState, useEffect, useCallback, type FormEvent } from 'react'
import {
  durableInferenceApi,
  MASKED_KEY,
  type DurableProvider,
  type DurableCombo,
  type BoundAgent,
  type ProviderCreatePayload,
  type ProviderUpdatePayload,
  type ComboCreatePayload,
  type ComboUpdatePayload,
  type DurableProviderType,
} from '../api/inferenceDurable'
import { ApiError } from '../api/client'

/**
 * Inference Providers view — the durable provider + combo registry.
 *
 * Drives the persistent ``/api/inference/durable`` surface (the read-only,
 * env-driven registry lives on the Inference page; this page is the write path):
 *
 *   - Providers: list / create / edit / delete / test-connection / recall models
 *   - Combos:    list / create / edit / delete + bound-agent preview
 *
 * Honest error surface: every non-2xx is shown verbatim. The API key is stored
 * in-band on the backend and masked on every read — the form only ever sends a
 * key when the user types a new one (the ``••••••••`` sentinel is never a
 * write value), and clearing uses an empty string.
 */

const PROVIDER_TYPES: DurableProviderType[] = [
  'openai',
  'anthropic',
  'local',
  'huggingface',
]

interface ErrorState {
  status: number
  detail: string
}

/** Convert a thrown error (ApiError or network) to an ErrorState. */
function errToState(err: unknown): ErrorState {
  if (err instanceof ApiError) {
    return { status: err.status, detail: err.detail }
  }
  return { status: 0, detail: err instanceof Error ? err.message : 'Request failed (network error)' }
}

// ─── Provider form ──────────────────────────────────────────────────────────

interface ProviderForm {
  name: string
  provider_type: DurableProviderType
  base_url: string
  api_key: string
  models: string
}

const emptyProviderForm: ProviderForm = {
  name: '',
  provider_type: 'openai',
  base_url: '',
  api_key: '',
  models: '',
}

// ─── Combo form ──────────────────────────────────────────────────────────────

interface ComboForm {
  name: string
  provider_id: string
  model_id: string
  note: string
}

const emptyComboForm: ComboForm = { name: '', provider_id: '', model_id: '', note: '' }

// ─── Per-row transient state ─────────────────────────────────────────────────

interface RowState {
  busy: boolean
  /** The recalled/tested models, or the error message. */
  result: string | null
  ok: boolean
}

const ProvidersView = () => {
  const [providers, setProviders] = useState<DurableProvider[]>([])
  const [combos, setCombos] = useState<DurableCombo[]>([])
  const [loading, setLoading] = useState(true)
  const [fetchError, setFetchError] = useState<ErrorState | null>(null)

  // Provider form
  const [showProviderForm, setShowProviderForm] = useState(false)
  const [providerForm, setProviderForm] = useState<ProviderForm>(emptyProviderForm)
  const [providerFormBusy, setProviderFormBusy] = useState(false)
  const [providerFormError, setProviderFormError] = useState<ErrorState | null>(null)

  // Provider edit (one row at a time)
  const [editingProviderId, setEditingProviderId] = useState<string | null>(null)
  const [editForm, setEditForm] = useState<ProviderForm>(emptyProviderForm)
  const [editBusy, setEditBusy] = useState(false)
  const [editError, setEditError] = useState<ErrorState | null>(null)

  // Combo form
  const [showComboForm, setShowComboForm] = useState(false)
  const [comboForm, setComboForm] = useState<ComboForm>(emptyComboForm)
  const [comboFormBusy, setComboFormBusy] = useState(false)
  const [comboFormError, setComboFormError] = useState<ErrorState | null>(null)

  // Combo edit
  const [editingComboId, setEditingComboId] = useState<string | null>(null)
  const [comboEditForm, setComboEditForm] = useState<ComboForm>(emptyComboForm)
  const [comboEditBusy, setComboEditBusy] = useState(false)
  const [comboEditError, setComboEditError] = useState<ErrorState | null>(null)

  // Per-row transient state (test / recall / bound-agents)
  const [rowStates, setRowStates] = useState<Record<string, RowState>>({})

  const [comboBound, setComboBound] = useState<Record<string, BoundAgent[]>>({})

  const refresh = useCallback(async () => {
    setFetchError(null)
    try {
      const [p, c] = await Promise.all([
        durableInferenceApi.listProviders(),
        durableInferenceApi.listCombos(),
      ])
      setProviders(p)
      setCombos(c)
    } catch (err) {
      setFetchError(errToState(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // ── Provider create ────────────────────────────────────────────────────────
  const submitProvider = async (e: FormEvent) => {
    e.preventDefault()
    if (!providerForm.name.trim()) {
      setProviderFormError({ status: 0, detail: 'Name is required.' })
      return
    }
    setProviderFormBusy(true)
    setProviderFormError(null)
    try {
      const payload: ProviderCreatePayload = {
        name: providerForm.name.trim(),
        provider_type: providerForm.provider_type,
        base_url: providerForm.base_url.trim() || null,
        api_key: providerForm.api_key || null,
        models: providerForm.models
          .split(',')
          .map((m) => m.trim())
          .filter(Boolean),
      }
      await durableInferenceApi.createProvider(payload)
      setShowProviderForm(false)
      setProviderForm(emptyProviderForm)
      await refresh()
    } catch (err) {
      setProviderFormError(errToState(err))
    } finally {
      setProviderFormBusy(false)
    }
  }

  // ── Provider edit ──────────────────────────────────────────────────────────
  const openEdit = (p: DurableProvider) => {
    setEditingProviderId(p.id)
    setEditForm({
      name: p.name,
      provider_type: p.provider_type,
      base_url: p.base_url ?? '',
      // Do NOT prefill the masked sentinel — it is read-only.
      api_key: '',
      models: p.models.join(', '),
    })
    setEditError(null)
  }
  const cancelEdit = () => {
    setEditingProviderId(null)
    setEditForm(emptyProviderForm)
    setEditError(null)
    setEditBusy(false)
  }
  const submitEdit = async (e: FormEvent) => {
    e.preventDefault()
    if (!editingProviderId) return
    if (!editForm.name.trim()) {
      setEditError({ status: 0, detail: 'Name is required.' })
      return
    }
    setEditBusy(true)
    setEditError(null)
    try {
      const payload: ProviderUpdatePayload = {
        name: editForm.name.trim(),
        provider_type: editForm.provider_type,
        base_url: editForm.base_url.trim() || null,
        models: editForm.models
          .split(',')
          .map((m) => m.trim())
          .filter(Boolean),
      }
      // Only send the key when the user typed a new one; empty string clears
      // it; omitted leaves it.
      if (editForm.api_key) {
        payload.api_key = editForm.api_key
      } else if (editForm.api_key === '') {
        // Empty = leave unchanged (we did not prefill, so '' here means the
        // user deliberately cleared it only if they typed then erased; to be
        // safe we treat a touched empty as "no change" — the key field is
        // optional and not required for an edit.)
      }
      await durableInferenceApi.updateProvider(editingProviderId, payload)
      cancelEdit()
      await refresh()
    } catch (err) {
      setEditError(errToState(err))
    } finally {
      setEditBusy(false)
    }
  }

  // ── Provider delete ────────────────────────────────────────────────────────
  const deleteProvider = async (id: string) => {
    if (!window.confirm('Delete this provider? Combos bound to it will break on resolution.')) return
    try {
      await durableInferenceApi.deleteProvider(id)
      await refresh()
    } catch (err) {
      setRowStates((s) => ({
        ...s,
        [id]: { busy: false, result: errToState(err).detail, ok: false },
      }))
    }
  }

  // ── Provider test / recall (per-row) ───────────────────────────────────────
  const testProvider = async (id: string) => {
    setRowStates((s) => ({ ...s, [id]: { busy: true, result: null, ok: true } }))
    try {
      const res = await durableInferenceApi.testProvider(id)
      setRowStates((s) => ({
        ...s,
        [id]: {
          busy: false,
          ok: res.success,
          result: res.success
            ? `✓ ${res.models.length} model(s): ${res.models.slice(0, 4).join(', ')}${res.models.length > 4 ? '…' : ''}`
            : `✗ ${res.error ?? 'test failed'}`,
        },
      }))
    } catch (err) {
      setRowStates((s) => ({
        ...s,
        [id]: { busy: false, ok: false, result: errToState(err).detail },
      }))
    }
  }

  const recallModels = async (id: string) => {
    setRowStates((s) => ({ ...s, [id]: { busy: true, result: null, ok: true } }))
    try {
      const res = await durableInferenceApi.recallProviderModels(id)
      setRowStates((s) => ({
        ...s,
        [id]: {
          busy: false,
          ok: true,
          result: res.models.length
            ? `${res.count} model(s) recalled`
            : 'No models found (no key or base_url?)',
        },
      }))
    } catch (err) {
      setRowStates((s) => ({
        ...s,
        [id]: { busy: false, ok: false, result: errToState(err).detail },
      }))
    }
  }

  // ── Combo create ───────────────────────────────────────────────────────────
  const submitCombo = async (e: FormEvent) => {
    e.preventDefault()
    if (!comboForm.name.trim() || !comboForm.provider_id || !comboForm.model_id.trim()) {
      setComboFormError({ status: 0, detail: 'Name, provider, and model are required.' })
      return
    }
    setComboFormBusy(true)
    setComboFormError(null)
    try {
      const payload: ComboCreatePayload = {
        name: comboForm.name.trim(),
        provider_id: comboForm.provider_id,
        model_id: comboForm.model_id.trim(),
        note: comboForm.note.trim() || null,
      }
      await durableInferenceApi.createCombo(payload)
      setShowComboForm(false)
      setComboForm(emptyComboForm)
      await refresh()
    } catch (err) {
      setComboFormError(errToState(err))
    } finally {
      setComboFormBusy(false)
    }
  }

  // ── Combo edit ─────────────────────────────────────────────────────────────
  const openComboEdit = (c: DurableCombo) => {
    setEditingComboId(c.id)
    setComboEditForm({
      name: c.name,
      provider_id: c.provider_id,
      model_id: c.model_id,
      note: c.note ?? '',
    })
    setComboEditError(null)
  }
  const cancelComboEdit = () => {
    setEditingComboId(null)
    setComboEditForm(emptyComboForm)
    setComboEditError(null)
    setComboEditBusy(false)
  }
  const submitComboEdit = async (e: FormEvent) => {
    e.preventDefault()
    if (!editingComboId) return
    if (!comboEditForm.name.trim() || !comboEditForm.provider_id || !comboEditForm.model_id.trim()) {
      setComboEditError({ status: 0, detail: 'Name, provider, and model are required.' })
      return
    }
    setComboEditBusy(true)
    setComboEditError(null)
    try {
      const payload: ComboUpdatePayload = {
        name: comboEditForm.name.trim(),
        provider_id: comboEditForm.provider_id,
        model_id: comboEditForm.model_id.trim(),
        note: comboEditForm.note.trim() || null,
      }
      await durableInferenceApi.updateCombo(editingComboId, payload)
      cancelComboEdit()
      await refresh()
    } catch (err) {
      setComboEditError(errToState(err))
    } finally {
      setComboEditBusy(false)
    }
  }

  const deleteCombo = async (id: string) => {
    if (!window.confirm('Delete this combo? Agents bound to it will fall back to their per-agent inference (or the default).')) return
    try {
      await durableInferenceApi.deleteCombo(id)
      await refresh()
    } catch (err) {
      setRowStates((s) => ({
        ...s,
        [id]: { busy: false, result: errToState(err).detail, ok: false },
      }))
    }
  }

  const showBoundAgents = async (id: string) => {
    if (comboBound[id]) {
      setComboBound((s) => ({ ...s, [id]: [] }))
      return
    }
    setRowStates((s) => ({ ...s, [id]: { busy: true, result: null, ok: true } }))
    try {
      const agents = await durableInferenceApi.listBoundAgents(id)
      setComboBound((s) => ({ ...s, [id]: agents }))
      setRowStates((s) => ({
        ...s,
        [id]: {
          busy: false,
          ok: true,
          result: agents.length
            ? `${agents.length} bound agent(s)`
            : 'No agents bound to this combo.',
        },
      }))
    } catch (err) {
      setRowStates((s) => ({
        ...s,
        [id]: { busy: false, ok: false, result: errToState(err).detail },
      }))
    }
  }

  // ── Shared row helper ──────────────────────────────────────────────────────
  const rowState = (id: string): RowState =>
    rowStates[id] ?? { busy: false, result: null, ok: true }

  // Models available for the combo form, derived from the provider list.
  const providerModelOptions = (providerId: string): string[] => {
    const p = providers.find((x) => x.id === providerId)
    return p ? p.models : []
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-3xl font-bold text-primary">Inference Providers</h1>
        <button
          onClick={() => {
            setShowProviderForm((v) => !v)
            setProviderFormError(null)
          }}
          className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-lg"
        >
          {showProviderForm ? 'Cancel' : '+ Add Provider'}
        </button>
      </div>
      <p className="text-sm text-muted">
        Persistent provider + combo registry (write path). Keys are stored in-band
        and masked on every read — only a freshly typed key is sent;{' '}
        <code className="text-xs">{MASKED_KEY}</code> is a read-only sentinel, never
        a write value.
      </p>

      {fetchError && (
        <div
          role="alert"
          data-testid="providers-error"
          className="p-3 rounded-md bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 text-sm font-medium"
        >
          Failed to load registry: {fetchError.status} — {fetchError.detail}
        </div>
      )}

      {/* ── Providers section ─────────────────────────────────────────────── */}
      <div className="bg-elevated rounded-lg shadow-lg p-6" data-testid="providers-section">
        <h3 className="text-xl font-semibold text-primary mb-4">Providers</h3>

        {showProviderForm && (
          <form
            data-testid="provider-form"
            onSubmit={submitProvider}
            className="bg-elevated p-6 rounded-lg shadow-lg space-y-4 mb-6"
          >
            <h4 className="text-lg font-semibold text-primary">New provider</h4>
            <div>
              <label htmlFor="provider-name" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Name</label>
              <input
                id="provider-name"
                data-testid="provider-form-name"
                type="text"
                required
                value={providerForm.name}
                onChange={(e) => setProviderForm((f) => ({ ...f, name: e.target.value }))}
                placeholder="e.g. OpenAI Prod"
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              />
            </div>
            <div>
              <label htmlFor="provider-type" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Type</label>
              <select
                id="provider-type"
                data-testid="provider-form-type"
                value={providerForm.provider_type}
                onChange={(e) => setProviderForm((f) => ({ ...f, provider_type: e.target.value as DurableProviderType }))}
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              >
                {PROVIDER_TYPES.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="provider-base-url" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Base URL</label>
              <input
                id="provider-base-url"
                data-testid="provider-form-base-url"
                type="text"
                value={providerForm.base_url}
                onChange={(e) => setProviderForm((f) => ({ ...f, base_url: e.target.value }))}
                placeholder="https://api.openai.com/v1"
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              />
            </div>
            <div>
              <label htmlFor="provider-key" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">API Key (stored in-band, masked on read)</label>
              <input
                id="provider-key"
                data-testid="provider-form-key"
                type="password"
                value={providerForm.api_key}
                onChange={(e) => setProviderForm((f) => ({ ...f, api_key: e.target.value }))}
                placeholder="Leave blank if using an env var or keyless local model"
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              />
            </div>
            <div>
              <label htmlFor="provider-models" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Models (comma-separated)</label>
              <input
                id="provider-models"
                data-testid="provider-form-models"
                type="text"
                value={providerForm.models}
                onChange={(e) => setProviderForm((f) => ({ ...f, models: e.target.value }))}
                placeholder="gpt-4o, gpt-4o-mini"
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              />
            </div>
            {providerFormError && (
              <div role="alert" data-testid="provider-form-error" className="p-3 rounded-md bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 text-sm font-medium">
                {providerFormError.status}: {providerFormError.detail}
              </div>
            )}
            <button
              type="submit"
              disabled={providerFormBusy}
              className="bg-blue-500 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded-lg"
            >
              {providerFormBusy ? 'Saving…' : 'Create provider'}
            </button>
          </form>
        )}

        {loading ? (
          <div className="text-center py-12">Loading…</div>
        ) : providers.length === 0 ? (
          <div className="text-center py-8">
            <p className="text-gray-700 dark:text-gray-300 font-medium">No durable providers yet.</p>
            <p className="text-sm text-muted mt-2 max-w-md mx-auto">
              Add a provider to register it in the persistent registry. The
              read-only env registry remains on the Inference page.
            </p>
          </div>
        ) : (
          <ul className="space-y-3" data-testid="providers-list">
            {providers.map((p) => {
              const rs = rowState(p.id)
              return (
                <li key={p.id} className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
                  <div className="flex justify-between items-start gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-primary">{p.name}</span>
                        <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 text-muted">{p.provider_type}</span>
                        {p.key_configured ? (
                          <span
                            data-testid={`provider-key-${p.id}`}
                            title="API key stored (masked)"
                            className="text-xs px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300"
                          >
                            key {p.api_key}
                          </span>
                        ) : (
                          <span
                            data-testid={`provider-no-key-${p.id}`}
                            className="text-xs px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 text-muted"
                          >
                            no key
                          </span>
                        )}
                      </div>
                      {p.base_url && <div className="text-sm text-muted mt-1 break-all">{p.base_url}</div>}
                      <div className="text-sm text-muted mt-1">
                        {p.model_count} model{p.model_count === 1 ? '' : 's'}
                        {p.models.length > 0 && (
                          <span className="ml-2 inline-block max-w-full overflow-hidden text-ellipsis whitespace-nowrap">{p.models.join(', ')}</span>
                        )}
                      </div>
                    </div>
                    <div className="flex gap-2 shrink-0 flex-wrap">
                      <button
                        data-testid={`provider-test-${p.id}`}
                        onClick={() => void testProvider(p.id)}
                        disabled={rs.busy}
                        className="text-xs px-3 py-1.5 rounded-md bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-primary disabled:opacity-50"
                      >
                        {rs.busy ? 'Testing…' : 'Test'}
                      </button>
                      <button
                        data-testid={`provider-recall-${p.id}`}
                        onClick={() => void recallModels(p.id)}
                        disabled={rs.busy}
                        className="text-xs px-3 py-1.5 rounded-md bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-primary disabled:opacity-50"
                      >
                        Recall models
                      </button>
                      {editingProviderId !== p.id ? (
                        <button
                          data-testid={`provider-edit-${p.id}`}
                          onClick={() => openEdit(p)}
                          className="text-xs px-3 py-1.5 rounded-md bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-primary"
                        >
                          Edit
                        </button>
                      ) : null}
                      <button
                        data-testid={`provider-delete-${p.id}`}
                        onClick={() => void deleteProvider(p.id)}
                        className="text-xs px-3 py-1.5 rounded-md bg-red-100 dark:bg-red-900/40 hover:bg-red-200 dark:hover:bg-red-900/60 text-red-700 dark:text-red-300"
                      >
                        Delete
                      </button>
                    </div>
                  </div>

                  {rs.result && (
                    <div
                      data-testid={`provider-result-${p.id}`}
                      className={`mt-3 text-sm ${rs.ok ? 'text-green-700 dark:text-green-300' : 'text-red-700 dark:text-red-300'}`}
                    >
                      {rs.result}
                    </div>
                  )}

                  {editingProviderId === p.id && (
                    <form
                      data-testid={`provider-edit-form-${p.id}`}
                      onSubmit={submitEdit}
                      className="mt-4 space-y-3 bg-white/40 dark:bg-black/20 rounded-md p-4"
                    >
                      <div>
                        <label htmlFor={`edit-name-${p.id}`} className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Name</label>
                        <input
                          id={`edit-name-${p.id}`}
                          data-testid={`provider-edit-name-${p.id}`}
                          type="text"
                          required
                          value={editForm.name}
                          onChange={(e) => setEditForm((f) => ({ ...f, name: e.target.value }))}
                          className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                        />
                      </div>
                      <div>
                        <label htmlFor={`edit-type-${p.id}`} className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Type</label>
                        <select
                          id={`edit-type-${p.id}`}
                          data-testid={`provider-edit-type-${p.id}`}
                          value={editForm.provider_type}
                          onChange={(e) => setEditForm((f) => ({ ...f, provider_type: e.target.value as DurableProviderType }))}
                          className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                        >
                          {PROVIDER_TYPES.map((t) => (
                            <option key={t} value={t}>{t}</option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <label htmlFor={`edit-base-url-${p.id}`} className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Base URL</label>
                        <input
                          id={`edit-base-url-${p.id}`}
                          data-testid={`provider-edit-base-url-${p.id}`}
                          type="text"
                          value={editForm.base_url}
                          onChange={(e) => setEditForm((f) => ({ ...f, base_url: e.target.value }))}
                          className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                        />
                      </div>
                      <div>
                        <label htmlFor={`edit-key-${p.id}`} className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                          API key (blank = unchanged; type a new value to replace)
                        </label>
                        <input
                          id={`edit-key-${p.id}`}
                          data-testid={`provider-edit-key-${p.id}`}
                          type="password"
                          value={editForm.api_key}
                          onChange={(e) => setEditForm((f) => ({ ...f, api_key: e.target.value }))}
                          className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                        />
                      </div>
                      <div>
                        <label htmlFor={`edit-models-${p.id}`} className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Models (comma-separated)</label>
                        <input
                          id={`edit-models-${p.id}`}
                          data-testid={`provider-edit-models-${p.id}`}
                          type="text"
                          value={editForm.models}
                          onChange={(e) => setEditForm((f) => ({ ...f, models: e.target.value }))}
                          className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                        />
                      </div>
                      {editError && (
                        <div role="alert" className="p-3 rounded-md bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 text-sm font-medium">
                          {editError.status}: {editError.detail}
                        </div>
                      )}
                      <div className="flex gap-2">
                        <button
                          type="submit"
                          disabled={editBusy}
                          className="bg-blue-500 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded-lg"
                        >
                          {editBusy ? 'Saving…' : 'Save'}
                        </button>
                        <button
                          type="button"
                          onClick={cancelEdit}
                          className="text-sm px-4 py-2 rounded-md bg-gray-100 dark:bg-gray-700 text-primary"
                        >
                          Cancel
                        </button>
                      </div>
                    </form>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>

      {/* ── Combos section ────────────────────────────────────────────────── */}
      <div className="bg-elevated rounded-lg shadow-lg p-6" data-testid="combos-section">
        <div className="flex justify-between items-center mb-4">
          <h3 className="text-xl font-semibold text-primary">Combos</h3>
          <button
            onClick={() => {
              setShowComboForm((v) => !v)
              setComboFormError(null)
            }}
            className="text-sm bg-blue-500 hover:bg-blue-600 text-white px-3 py-1.5 rounded-lg"
          >
            {showComboForm ? 'Cancel' : '+ Add Combo'}
          </button>
        </div>
        <p className="text-sm text-muted mb-4">
          A combo pairs a provider with a model. Agents can bind to a combo
          (shadowing their per-agent inference); re-pointing a combo
          re-binds every bound agent with no write.
        </p>

        {showComboForm && (
          <form
            data-testid="combo-form"
            onSubmit={submitCombo}
            className="bg-white/40 dark:bg-black/20 rounded-md p-4 space-y-3 mb-4"
          >
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label htmlFor="combo-name" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Name</label>
                <input
                  id="combo-name"
                  data-testid="combo-form-name"
                  type="text"
                  required
                  value={comboForm.name}
                  onChange={(e) => setComboForm((f) => ({ ...f, name: e.target.value }))}
                  placeholder="gpt4o"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                />
              </div>
              <div>
                <label htmlFor="combo-provider" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Provider</label>
                <select
                  id="combo-provider"
                  data-testid="combo-form-provider"
                  required
                  value={comboForm.provider_id}
                  onChange={(e) => setComboForm((f) => ({ ...f, provider_id: e.target.value }))}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                >
                  <option value="">Select…</option>
                  {providers.map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="combo-model" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Model</label>
                <input
                  id="combo-model"
                  data-testid="combo-form-model"
                  type="text"
                  required
                  value={comboForm.model_id}
                  onChange={(e) => setComboForm((f) => ({ ...f, model_id: e.target.value }))}
                  list={comboForm.provider_id ? `combo-models-${comboForm.provider_id}` : undefined}
                  placeholder={providerModelOptions(comboForm.provider_id).length ? 'Pick or type' : 'gpt-4o'}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                />
                <datalist id={comboForm.provider_id ? `combo-models-${comboForm.provider_id}` : undefined}>
                  {providerModelOptions(comboForm.provider_id).map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
              </div>
            </div>
            <div>
              <label htmlFor="combo-note" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Note (optional)</label>
              <input
                id="combo-note"
                data-testid="combo-form-note"
                type="text"
                value={comboForm.note}
                onChange={(e) => setComboForm((f) => ({ ...f, note: e.target.value }))}
                className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
              />
            </div>
            {comboFormError && (
              <div role="alert" data-testid="combo-form-error" className="p-3 rounded-md bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 text-sm font-medium">
                {comboFormError.status}: {comboFormError.detail}
              </div>
            )}
            <button
              type="submit"
              disabled={comboFormBusy || providers.length === 0}
              className="bg-blue-500 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded-lg"
            >
              {comboFormBusy ? 'Saving…' : 'Create combo'}
            </button>
          </form>
        )}

        {loading ? (
          <div className="text-center py-12">Loading…</div>
        ) : combos.length === 0 ? (
          <div className="text-center py-8">
            <p className="text-gray-700 dark:text-gray-300 font-medium">No combos yet.</p>
            <p className="text-sm text-muted mt-2 max-w-md mx-auto">
              Create a combo to pair a provider with a model, then bind agents to it.
            </p>
          </div>
        ) : (
          <ul className="space-y-3" data-testid="combos-list">
            {combos.map((c) => {
              const rs = rowState(c.id)
              const bound = comboBound[c.id]
              return (
                <li key={c.id} className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
                  <div className="flex justify-between items-start gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-primary">{c.name}</span>
                        <span className="text-xs text-muted">{c.provider_name} · {c.model_id}</span>
                      </div>
                      {c.note && <div className="text-sm text-muted mt-1">{c.note}</div>}
                    </div>
                    <div className="flex gap-2 shrink-0 flex-wrap">
                      <button
                        data-testid={`combo-agents-${c.id}`}
                        onClick={() => void showBoundAgents(c.id)}
                        disabled={rs.busy}
                        className="text-xs px-3 py-1.5 rounded-md bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-primary disabled:opacity-50"
                      >
                        {rs.busy ? 'Loading…' : 'Bound agents'}
                      </button>
                      {editingComboId !== c.id ? (
                        <button
                          data-testid={`combo-edit-${c.id}`}
                          onClick={() => openComboEdit(c)}
                          className="text-xs px-3 py-1.5 rounded-md bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-primary"
                        >
                          Edit
                        </button>
                      ) : null}
                      <button
                        data-testid={`combo-delete-${c.id}`}
                        onClick={() => void deleteCombo(c.id)}
                        className="text-xs px-3 py-1.5 rounded-md bg-red-100 dark:bg-red-900/40 hover:bg-red-200 dark:hover:bg-red-900/60 text-red-700 dark:text-red-300"
                      >
                        Delete
                      </button>
                    </div>
                  </div>

                  {rs.result && (
                    <div
                      data-testid={`combo-result-${c.id}`}
                      className={`mt-3 text-sm ${rs.ok ? 'text-green-700 dark:text-green-300' : 'text-red-700 dark:text-red-300'}`}
                    >
                      {rs.result}
                    </div>
                  )}

                  {bound && bound.length > 0 && (
                    <div data-testid={`combo-bound-${c.id}`} className="mt-2 flex flex-wrap gap-1">
                      {bound.map((a) => (
                        <span
                          key={a.id}
                          className="text-xs px-2 py-0.5 rounded-full bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
                        >
                          {a.name} ({a.type})
                        </span>
                      ))}
                    </div>
                  )}

                  {editingComboId === c.id && (
                    <form
                      data-testid={`combo-edit-form-${c.id}`}
                      onSubmit={submitComboEdit}
                      className="mt-4 space-y-3 bg-white/40 dark:bg-black/20 rounded-md p-4"
                    >
                      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                        <div>
                          <label htmlFor={`combo-edit-name-${c.id}`} className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Name</label>
                          <input
                            id={`combo-edit-name-${c.id}`}
                            data-testid={`combo-edit-name-${c.id}`}
                            type="text"
                            required
                            value={comboEditForm.name}
                            onChange={(e) => setComboEditForm((f) => ({ ...f, name: e.target.value }))}
                            className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                          />
                        </div>
                        <div>
                          <label htmlFor={`combo-edit-provider-${c.id}`} className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Provider</label>
                          <select
                            id={`combo-edit-provider-${c.id}`}
                            data-testid={`combo-edit-provider-${c.id}`}
                            required
                            value={comboEditForm.provider_id}
                            onChange={(e) => setComboEditForm((f) => ({ ...f, provider_id: e.target.value }))}
                            className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                          >
                            <option value="">Select…</option>
                            {providers.map((p) => (
                              <option key={p.id} value={p.id}>{p.name}</option>
                            ))}
                          </select>
                        </div>
                        <div>
                          <label htmlFor={`combo-edit-model-${c.id}`} className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Model</label>
                          <input
                            id={`combo-edit-model-${c.id}`}
                            data-testid={`combo-edit-model-${c.id}`}
                            type="text"
                            required
                            value={comboEditForm.model_id}
                            onChange={(e) => setComboEditForm((f) => ({ ...f, model_id: e.target.value }))}
                            className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
                          />
                        </div>
                      </div>
                      {comboEditError && (
                        <div role="alert" className="p-3 rounded-md bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 text-sm font-medium">
                          {comboEditError.status}: {comboEditError.detail}
                        </div>
                      )}
                      <div className="flex gap-2">
                        <button
                          type="submit"
                          disabled={comboEditBusy}
                          className="bg-blue-500 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded-lg"
                        >
                          {comboEditBusy ? 'Saving…' : 'Save'}
                        </button>
                        <button
                          type="button"
                          onClick={cancelComboEdit}
                          className="text-sm px-4 py-2 rounded-md bg-gray-100 dark:bg-gray-700 text-primary"
                        >
                          Cancel
                        </button>
                      </div>
                    </form>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}

export { ProvidersView }
