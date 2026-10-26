import { useState, useEffect, FormEvent } from 'react'
import { goalsApi, apiFetch } from '../api/client'

/**
 * Normalized goal shape consumed by the renderer.
 *
 * The real §46 backend (`schemas/goals.py`) returns `title` / `status`
 * (active|paused|achieved|abandoned) / `progress` as a 0..1 fraction /
 * `deadline`. The in-browser mock fixture (`mockClient.ts`) returns `name` /
 * `dueDate` and `progress` as 0..100. Both shapes are normalized so the view
 * renders honestly against either (GL-2).
 */
interface Goal {
  id: string
  name: string
  description: string
  status: string
  progressPct: number
  dueDate: string | null
  priority: number
}

/**
 * Progress normalizer: values ≤ 1 are treated as 0..1 fractions and scaled to
 * a percentage; anything larger is already a percentage. Non-numeric input
 * degrades to 0.
 */
function toPercent(p: unknown): number {
  const n = typeof p === 'number' ? p : parseFloat(String(p ?? ''))
  if (!Number.isFinite(n) || n < 0) return 0
  const pct = n <= 1 ? Math.round(n * 100) : Math.round(n)
  return Math.max(0, Math.min(100, pct))
}

function normalizeGoal(g: any, idx: number): Goal {
  return {
    id: String(g?.id ?? g?.goal_id ?? `goal-${idx}`),
    name: g?.title ?? g?.name ?? 'Untitled goal',
    description: g?.description ?? '',
    status: g?.status ?? 'active',
    progressPct: toPercent(g?.progress),
    dueDate: g?.deadline ?? g?.dueDate ?? null,
    priority: Number.isFinite(Number(g?.priority)) ? Number(g?.priority) : 0,
  }
}

function formatDate(v: string | null): string {
  if (!v) return 'No due date'
  // r3 walk (GL-2): deadlines are stored as UTC instants (`2026-11-30T00:00:00Z`).
  // Formatting the instant with the browser's local zone shifts the calendar
  // date back a day west of UTC (a Nov 30 deadline rendered as "Nov 29").
  // Render the stored calendar date instead.
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v)
  if (m) {
    const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
    return d.toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      timeZone: 'UTC',
    })
  }
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/**
 * Render an API failure as `<status> — <detail>`. Duck-types the shared
 * `ApiError { status, detail }` contract so it works whether or not the
 * client has adopted `ApiError` yet; falls back to `Error.message`.
 */
function formatApiError(err: unknown): string {
  const e = err as { status?: number; detail?: string; message?: string } | null
  if (e && typeof e.status === 'number' && typeof e.detail === 'string') {
    return `${e.status} — ${e.detail}`
  }
  if (e && typeof e.message === 'string' && e.message) return e.message
  return 'Request failed'
}

type ApiResult = {
  ok?: boolean
  status?: number
  detail?: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data?: any
}

/**
 * Defensive status guard for the transitional client contract. The in-flight
 * `ApiError` contract (sibling task) will make `realApi` reject on non-2xx;
 * until it lands, `realApi` still *resolves* `{ data, status }` on 5xx.
 */
function assertOk(res: ApiResult | undefined | null, context: string): ApiResult {
  if (res && typeof res.status === 'number' && res.status >= 400 && !res.ok) {
    const status = res.status
    const detail = res.detail
    const err: any = new Error(
      `${status} — ${typeof detail === 'string' && detail ? detail : `${context}: HTTP ${status}`}`,
    )
    err.status = status
    err.detail = typeof detail === 'string' && detail ? detail : `${context}: HTTP ${status}`
    throw err
  }
  if (!res) {
    const err: any = new Error(`${context}: no response`)
    err.status = 500
    err.detail = `${context}: no response`
    throw err
  }
  return res
}

/** Build a `${status} — ${detail}` error from a non-2xx `Response`. */
async function rejectFromResponse(res: Response): Promise<never> {
  let detail = `HTTP ${res.status}`
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown }
    if (body && typeof body.detail === 'string' && body.detail) detail = body.detail
    else if (body && typeof body.message === 'string' && body.message) detail = body.message
  } catch {
    // non-JSON body — keep the status code
  }
  throw new Error(`${res.status} — ${detail}`)
}

const goalStatusColor = (status: string): string => {
  switch (status) {
    case 'active':
    case 'ACTIVE':
      return 'bg-blue-500'
    case 'achieved':
    case 'completed':
    case 'done':
      return 'bg-green-500'
    case 'paused':
    case 'pending':
      return 'bg-yellow-500'
    case 'abandoned':
    case 'cancelled':
      return 'bg-gray-500'
    default:
      return 'bg-gray-400'
  }
}

const GoalsView = () => {
  const [goals, setGoals] = useState<Goal[]>([])
  const [loading, setLoading] = useState(true)
  const [listError, setListError] = useState<string | null>(null)

  // Create-flow state (GL-1).
  const [formOpen, setFormOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [form, setForm] = useState({
    title: '',
    description: '',
    priority: '0',
    deadline: '',
  })

  // Delete-flow state (GL-3). The delete is destructive, so it confirms
  // inline: the first tap reveals the row identity plus Cancel/Confirm, and
  // the DELETE is only issued on explicit confirm. No window.confirm — it
  // is invisible to the e2e harness and to keyboard-only users.
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const loadGoals = async () => {
    const response = await assertOk(await goalsApi.list(), 'goals')
    const rows = (response.data ?? []) as any[]
    setGoals(rows.map((g, i) => normalizeGoal(g, i)))
    setListError(null)
  }

  useEffect(() => {
    const fetchGoals = async () => {
      try {
        await loadGoals()
      } catch (error) {
        setListError(formatApiError(error))
      } finally {
        setLoading(false)
      }
    }

    fetchGoals()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const resetForm = () => {
    setForm({ title: '', description: '', priority: '0', deadline: '' })
    setFormError(null)
  }

  const openForm = () => {
    resetForm()
    setFormOpen(true)
  }

  const closeForm = () => {
    if (submitting) return
    setFormOpen(false)
    setFormError(null)
  }

  /**
   * POST a new goal.
   *
   * NOTE (follow-up): `goalsApi` in `client.ts` currently only exposes
   * `list`/`get` — there is no `create` method. Per the integration contract
   * `client.ts` is out of scope for this task, so the create is issued
   * directly through the exported `apiFetch` transport (which injects the
   * `Bearer` token from sessionStorage and respects `VITE_API_BASE`). Once
   * a sibling adds `goalsApi.create`, this should delegate to it instead.
   */
  const submitGoal = async (e: FormEvent) => {
    e.preventDefault()
    if (!form.title.trim()) {
      setFormError('A goal name is required.')
      return
    }
    setSubmitting(true)
    setFormError(null)
    try {
      const payload: Record<string, unknown> = {
        title: form.title.trim(),
        priority: form.priority === '' ? 0 : Number(form.priority),
      }
      if (form.description.trim()) payload.description = form.description.trim()
      if (form.deadline) payload.deadline = form.deadline

      const res = await apiFetch('/goals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) await rejectFromResponse(res)

      // Refresh the list so the new goal renders (GL-1).
      await loadGoals()
      setFormOpen(false)
    } catch (err) {
      setFormError(formatApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  const requestDelete = (goalId: string) => {
    if (deleting) return
    setDeleteError(null)
    setConfirmDelete(goalId)
  }

  const cancelDelete = () => {
    if (deleting) return
    setConfirmDelete(null)
  }

  /**
   * Issue `DELETE /goals/{id}` (backend: 204 on success, 404 unknown id).
   * On success the row is removed locally; on failure the `ApiError`
   * `status — detail` lands in the delete-error banner and the row stays.
   */
  const confirmDeleteGoal = async () => {
    const goalId = confirmDelete
    if (!goalId) return
    setDeleting(true)
    setDeleteError(null)
    try {
      assertOk(await goalsApi.delete(goalId), 'delete goal')
      setGoals((prev) => prev.filter((g) => g.id !== goalId))
      setConfirmDelete(null)
    } catch (err) {
      setDeleteError(formatApiError(err))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap justify-between items-center gap-4">
        <h1 className="text-3xl font-bold text-primary">Goals</h1>
        <button
          type="button"
          onClick={openForm}
          className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-medium"
        >
          + New Goal
        </button>
      </div>

      {listError && (
        <div
          role="alert"
          className="text-red-700 dark:text-red-300 bg-red-100 dark:bg-red-900/40 border border-red-300 dark:border-red-700 p-4 rounded-lg"
        >
          <p className="font-semibold mb-1">Goals unavailable</p>
          <p className="text-sm font-mono break-words">{listError}</p>
        </div>
      )}

      {deleteError && (
        <div
          role="alert"
          className="text-red-700 dark:text-red-300 bg-red-100 dark:bg-red-900/40 border border-red-300 dark:border-red-700 p-4 rounded-lg"
        >
          <p className="font-semibold mb-1">Delete failed</p>
          <p className="text-sm font-mono break-words">{deleteError}</p>
        </div>
      )}

      {formOpen && (
        <form
          onSubmit={submitGoal}
          className="bg-elevated rounded-lg shadow-lg p-6 space-y-4"
          aria-label="Create a new goal"
        >
          <h3 className="text-lg font-semibold text-primary">
            New goal
          </h3>

          <div>
            <label
              htmlFor="goal-title"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Name <span className="text-red-500" aria-hidden="true">*</span>
            </label>
            <input
              id="goal-title"
              type="text"
              value={form.title}
              onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
              placeholder="e.g. Reduce context compilation latency below 1s p95"
              required
              className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label
              htmlFor="goal-description"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Description
            </label>
            <textarea
              id="goal-description"
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              rows={3}
              placeholder="What outcome does this goal describe?"
              className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label
                htmlFor="goal-priority"
                className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
              >
                Priority (0–10)
              </label>
              <input
                id="goal-priority"
                type="number"
                min={0}
                max={10}
                value={form.priority}
                onChange={(e) => setForm((f) => ({ ...f, priority: e.target.value }))}
                className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label
                htmlFor="goal-deadline"
                className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
              >
                Due date
              </label>
              <input
                id="goal-deadline"
                type="date"
                value={form.deadline}
                onChange={(e) => setForm((f) => ({ ...f, deadline: e.target.value }))}
                className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-primary focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          </div>

          {formError && (
            <div
              role="alert"
              className="text-red-700 dark:text-red-300 bg-red-100 dark:bg-red-900/40 border border-red-300 dark:border-red-700 px-3 py-2 rounded-lg text-sm"
            >
              {formError}
            </div>
          )}

          <div className="flex items-center justify-end gap-3">
            <button
              type="button"
              onClick={closeForm}
              disabled={submitting}
              className="px-4 py-2 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-medium disabled:opacity-50"
            >
              {submitting ? 'Creating…' : 'Create goal'}
            </button>
          </div>
        </form>
      )}

      <div className="text-muted text-sm">
        {goals.length} total goal{goals.length === 1 ? '' : 's'}
      </div>

      {loading ? (
        <div className="text-center py-12" role="status">
          Loading goals…
        </div>
      ) : goals.length === 0 ? (
        <div className="bg-elevated rounded-lg shadow-lg p-8 text-center">
          <p className="text-sm text-muted max-w-md mx-auto">
            No goals yet. Goals are the targets NerveCenter works toward — create
            one with “+ New Goal”, and its progress, status, and due date will
            show up here.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {goals.map((goal) => (
            <div
              key={goal.id}
              className="bg-elevated p-6 rounded-lg shadow-lg"
              data-testid={`goal-card-${goal.id}`}
            >
              <div className="flex items-start justify-between mb-4">
                <h3 className="text-xl font-semibold text-primary">
                  {goal.name}
                </h3>
                <span
                  role="img"
                  aria-label={`Status: ${goal.status}`}
                  className="shrink-0 flex items-center px-2 py-1 rounded text-xs font-medium text-white"
                >
                  <span
                    className={`w-2 h-2 rounded-full mr-1.5 ${goalStatusColor(goal.status)}`}
                    aria-hidden="true"
                  />
                  {goal.status}
                </span>
              </div>
              {goal.description && (
                <p className="text-gray-600 dark:text-gray-300 mb-4 text-sm">
                  {goal.description}
                </p>
              )}
              <div className="space-y-2">
                <div className="flex justify-between text-sm">
                  <span className="text-muted">Progress</span>
                  <span className="text-primary tabular-nums">
                    {goal.progressPct}%
                  </span>
                </div>
                <div
                  className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2 overflow-hidden"
                  role="progressbar"
                  aria-valuenow={goal.progressPct}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`${goal.name} progress`}
                >
                  <div
                    className={`h-2 rounded-full ${
                      goal.progressPct === 100 ? 'bg-green-500' : 'bg-blue-500'
                    }`}
                    style={{ width: `${goal.progressPct}%` }}
                  />
                </div>
              </div>
              <div className="mt-4 pt-4 border-t border-line">
                <div className="flex justify-between text-sm">
                  <span className="text-muted">Due</span>
                  <span className="text-primary">{formatDate(goal.dueDate)}</span>
                </div>
                <div className="flex justify-between text-sm mt-2">
                  <span className="text-muted">Priority</span>
                  <span className="text-primary tabular-nums">
                    P{goal.priority}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-3 mt-3 pt-3 border-t border-line">
                  {confirmDelete === goal.id ? (
                    <>
                      <span className="text-sm text-red-700 dark:text-red-300">
                        Delete “{goal.name}”?
                      </span>
                      <div className="flex items-center gap-2 shrink-0">
                        <button
                          type="button"
                          onClick={cancelDelete}
                          disabled={deleting}
                          className="px-3 py-1.5 rounded-lg text-xs font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                          data-testid={`goal-cancel-delete-${goal.id}`}
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          onClick={confirmDeleteGoal}
                          disabled={deleting}
                          className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-600 hover:bg-red-700 text-white disabled:opacity-50"
                          data-testid={`goal-confirm-delete-${goal.id}`}
                        >
                          {deleting ? 'Deleting…' : 'Delete'}
                        </button>
                      </div>
                    </>
                  ) : (
                    <button
                      type="button"
                      onClick={() => requestDelete(goal.id)}
                      disabled={deleting}
                      className="text-sm text-red-700 dark:text-red-300 hover:underline disabled:opacity-50"
                      data-testid={`goal-delete-${goal.id}`}
                    >
                      Delete goal
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export { GoalsView }
