import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { agentsApi, type AgentRun, type AgentSchedule } from '../api/agents'
import { ApiError } from '../api/client'

/**
 * Schedule editor, run-now button and run history for one persisted agent.
 * Opened per card so no extra requests fire during the list render.
 */
const EMPTY: AgentSchedule = {
  mode: 'on_demand',
  interval_seconds: null,
  daily_time: null,
  timezone: null,
  priority: 'background',
  enabled: false,
}

const errText = (e: unknown): string =>
  e instanceof ApiError ? `${e.status} — ${e.detail}` : 'Request failed (network error)'

export function AgentRunnerPanel({
  agentId,
  agentName,
  onChanged,
}: {
  agentId: string
  agentName: string
  onChanged: () => void
}) {
  const [schedule, setSchedule] = useState<AgentSchedule>(EMPTY)
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)

  const load = useCallback(async () => {
    try {
      const [sched, history] = await Promise.all([
        agentsApi.getSchedule(agentId),
        agentsApi.runs(agentId),
      ])
      setSchedule(sched)
      setRuns(history)
      setError(null)
    } catch (e) {
      setError(errText(e))
    }
  }, [agentId])

  useEffect(() => {
    void load()
  }, [load])

  const save = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setSaved(false)
    try {
      setSchedule(await agentsApi.putSchedule(agentId, schedule))
      setSaved(true)
      setError(null)
    } catch (err) {
      setError(errText(err))
    } finally {
      setBusy(false)
    }
  }

  const runNow = async () => {
    setBusy(true)
    try {
      await agentsApi.run(agentId)
      setError(null)
      await load()
      onChanged()
    } catch (err) {
      setError(errText(err))
    } finally {
      setBusy(false)
    }
  }

  const field = 'border rounded px-2 py-1 bg-transparent text-sm'

  return (
    <div
      data-testid="agent-runner-panel"
      className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-4 space-y-4"
    >
      {error && (
        <div role="alert" data-testid="agent-runner-error" className="text-sm text-red-600">
          {error}
        </div>
      )}
      <form onSubmit={save} className="space-y-3" aria-label={`Schedule for ${agentName}`}>
        <div className="flex flex-wrap gap-3 items-end">
          <label className="text-sm">
            Cadence
            <select
              data-testid="schedule-mode"
              className={`${field} block`}
              value={schedule.mode}
              onChange={(e) =>
                setSchedule({
                  ...schedule,
                  mode: e.target.value as AgentSchedule['mode'],
                  enabled: e.target.value !== 'on_demand',
                  interval_seconds: e.target.value === 'interval' ? schedule.interval_seconds ?? 3600 : null,
                  daily_time: e.target.value === 'daily' ? schedule.daily_time ?? '08:00' : null,
                  timezone: e.target.value === 'daily' ? schedule.timezone ?? 'UTC' : null,
                })
              }
            >
              <option value="on_demand">On demand only</option>
              <option value="interval">Every N minutes</option>
              <option value="daily">Daily</option>
            </select>
          </label>
          {schedule.mode === 'interval' && (
            <label className="text-sm">
              Minutes
              <input
                data-testid="schedule-minutes"
                type="number"
                min={1}
                className={`${field} block w-24`}
                value={Math.round((schedule.interval_seconds ?? 3600) / 60)}
                onChange={(e) =>
                  setSchedule({ ...schedule, interval_seconds: Math.max(1, Number(e.target.value)) * 60 })
                }
              />
            </label>
          )}
          {schedule.mode === 'daily' && (
            <>
              <label className="text-sm">
                Time
                <input
                  type="time"
                  className={`${field} block`}
                  value={schedule.daily_time ?? '08:00'}
                  onChange={(e) => setSchedule({ ...schedule, daily_time: e.target.value })}
                />
              </label>
              <label className="text-sm">
                Timezone
                <input
                  className={`${field} block w-44`}
                  value={schedule.timezone ?? 'UTC'}
                  onChange={(e) => setSchedule({ ...schedule, timezone: e.target.value })}
                />
              </label>
            </>
          )}
          <label className="text-sm">
            Priority
            <select
              className={`${field} block`}
              value={schedule.priority}
              onChange={(e) =>
                setSchedule({ ...schedule, priority: e.target.value as AgentSchedule['priority'] })
              }
            >
              <option value="background">Background (slows down first)</option>
              <option value="time_critical">Time critical</option>
            </select>
          </label>
          <button
            type="submit"
            disabled={busy}
            className="bg-blue-500 hover:bg-blue-600 text-white px-3 py-1 rounded text-sm"
          >
            Save schedule
          </button>
          <button
            type="button"
            data-testid="agent-run-now"
            disabled={busy}
            onClick={runNow}
            className="border border-blue-500 text-blue-600 dark:text-blue-400 px-3 py-1 rounded text-sm"
          >
            Run now
          </button>
        </div>
        {saved && <div className="text-sm text-green-600">Schedule saved.</div>}
        {schedule.next_run_at && (
          <div className="text-xs text-muted">Next run: {schedule.next_run_at}</div>
        )}
      </form>
      <div data-testid="agent-run-history">
        <div className="text-sm font-medium text-primary mb-2">
          Run history ({runs.filter((r) => r.status === 'completed').length} completed,{' '}
          {runs.filter((r) => r.status === 'failed').length} failed)
        </div>
        {runs.length === 0 ? (
          <div className="text-sm text-muted">No runs yet.</div>
        ) : (
          <ul className="space-y-2">
            {runs.map((r) => (
              <li key={r.id} className="text-sm border rounded p-2">
                <div className="flex justify-between">
                  <span className="font-medium">{r.status}</span>
                  <span className="text-xs text-muted">
                    {r.started_at ?? ''} · {r.trigger}
                    {r.latency_ms != null ? ` · ${r.latency_ms} ms` : ''}
                  </span>
                </div>
                {r.skills_status && r.skills_status !== 'unconfigured' && (
                  <div className="text-xs text-muted mt-1" data-testid="agent-run-skills">
                    Skills: {r.skills_status}
                    {(r.skills ?? []).length > 0 &&
                      ` · ${(r.skills ?? []).map((s) => `${s.id}@${s.revision}`).join(', ')}`}
                  </div>
                )}
                {(r.output || r.error) && (
                  <pre className="whitespace-pre-wrap text-xs mt-1 max-h-40 overflow-auto">
                    {r.error ?? r.output}
                  </pre>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
