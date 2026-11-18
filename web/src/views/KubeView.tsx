import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  kubeApi,
  type KubeIssue,
  type KubeMode,
  type KubeOverride,
  type KubeSettings,
  type KubeStatus,
  type WasteItem,
  type WasteReport,
  type WasteSection,
} from '../api/kube'

/**
 * Kube agent (NS-ADR-120). The hourly check lists what is wrong; the agent only proposes
 * plans, and every plan step still needs the owner's approval on the Infrastructure page.
 * Protected namespaces are diagnose-only until the owner overrides one issue here.
 */
const errText = (e: unknown) => (e instanceof ApiError ? `${e.status} — ${e.detail}` : 'Request failed')
const btn = 'rounded px-3 py-1.5 text-sm font-medium disabled:opacity-50'
const primary = `${btn} bg-blue-600 text-white hover:bg-blue-700`
const secondary = `${btn} border border-line text-primary hover:bg-hover`
const input = 'rounded border border-line bg-surface px-2 py-1 text-sm'

const OVERRIDE_TEXT =
  'Lets the agent propose a fix plan for this issue for 24 hours. Every step still needs your approval.'

const MODE_LABEL: Record<KubeMode, string> = {
  normal: 'normal',
  known_down: 'known down',
  diagnose_only: 'protected: diagnosing only',
}
const MODE_CLASS: Record<KubeMode, string> = {
  normal: 'bg-sky-600 text-white',
  known_down: 'bg-gray-500 text-white',
  diagnose_only: 'bg-amber-500 text-black',
}

const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : 'never')

function Chips({ label, value, onChange, testId }: {
  label: string
  value: string[]
  onChange: (next: string[]) => void
  testId: string
}) {
  const [draft, setDraft] = useState('')
  const add = () => {
    const name = draft.trim()
    if (name && !value.includes(name)) onChange([...value, name])
    setDraft('')
  }
  return (
    <div data-testid={testId} className="space-y-1">
      <h3 className="text-sm font-semibold text-primary">{label}</h3>
      <div className="flex flex-wrap gap-1">
        {value.map((ns) => (
          <span key={ns} className="flex items-center gap-1 rounded bg-hover px-2 py-0.5 text-xs">
            {ns}
            <button aria-label={`Remove ${ns}`} onClick={() => onChange(value.filter((v) => v !== ns))}>×</button>
          </span>
        ))}
        {value.length === 0 && <span className="text-xs text-secondary">none</span>}
      </div>
      <div className="flex gap-2">
        <input
          aria-label={`Add to ${label}`}
          className={input}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') add() }}
          placeholder="namespace"
        />
        <button className={secondary} onClick={add}>Add</button>
      </div>
    </div>
  )
}

function WasteBlock({ title, section, note }: { title: string; section: WasteSection; note?: string }) {
  const line = (item: WasteItem) =>
    Object.entries(item).map(([k, v]) => `${k}=${String(v)}`).join(' ')
  return (
    <div className="space-y-1">
      <h3 className="text-sm font-semibold text-primary">{title} ({section.count}){note ? ` — ${note}` : ''}</h3>
      <ul className="space-y-0.5 text-xs text-secondary">
        {section.items.slice(0, 10).map((item, i) => <li key={i}>{line(item)}</li>)}
        {section.items.length === 0 && <li>none</li>}
      </ul>
    </div>
  )
}

const NUMBER_FIELDS: [keyof KubeSettings, string][] = [
  ['retention_daily', 'Daily backups kept'],
  ['retention_weekly', 'Weekly backups kept'],
  ['daily_run_cap', 'Agent runs per day'],
  ['issue_cooldown_hours', 'Cooldown per issue (hours)'],
  ['override_hours', 'Override lasts (hours)'],
]

export function KubeView() {
  const [status, setStatus] = useState<KubeStatus | null>(null)
  const [issues, setIssues] = useState<KubeIssue[]>([])
  const [overrides, setOverrides] = useState<KubeOverride[]>([])
  const [waste, setWaste] = useState<{ created_at: string; data: WasteReport } | null>(null)
  const [draft, setDraft] = useState<KubeSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const [st, is, w] = await Promise.all([kubeApi.status(), kubeApi.issues(), kubeApi.waste()])
      setStatus(st)
      setDraft(st.settings)
      setIssues(is.issues)
      setOverrides(is.overrides)
      setWaste(w)
      setError(null)
    } catch (e) {
      setError(errText(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await fn()
      await load()
    } catch (e) {
      setError(errText(e))
    } finally {
      setBusy(false)
    }
  }

  const override = (issue: KubeIssue) => {
    if (!window.confirm(`${OVERRIDE_TEXT}\n\n${issue.key}`)) return
    return act(() => kubeApi.override(issue.key))
  }
  const saveSettings = () => {
    if (!draft) return
    const { protected_namespaces, known_down, retention_daily, retention_weekly, daily_run_cap,
      issue_cooldown_hours, override_hours, prometheus_url } = draft
    return act(() => kubeApi.saveSettings({
      protected_namespaces, known_down, retention_daily, retention_weekly, daily_run_cap,
      issue_cooldown_hours, override_hours, prometheus_url: prometheus_url || null,
    }))
  }

  const down = issues.filter((i) => i.mode === 'known_down')
  const current = issues.filter((i) => i.mode !== 'known_down')

  return (
    <div className="space-y-6 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-bold text-primary">Kube</h1>
        <div className="flex items-center gap-3 text-sm text-secondary">
          <span data-testid="kube-last-check">Last check: {when(status?.last_check_at)}</span>
          <button data-testid="kube-check-now" className={primary} disabled={busy} onClick={() => act(() => kubeApi.checkNow())}>
            Check now
          </button>
        </div>
      </div>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {status && (
        <p
          data-testid="kube-coverage"
          className={`text-sm ${status.coverage && status.coverage.percent >= 90 && status.coverage.unreadable.length === 0 && status.coverage.cluster_unreadable.length === 0 ? 'text-secondary' : 'text-red-600'}`}
        >
          Coverage: {status.coverage ? status.coverage.line : 'unknown (no check has recorded coverage yet)'}
          {status.coverage && status.coverage.unreadable.length > 0 && ' - unread namespaces are unknown, not healthy.'}
        </p>
      )}
      <p className="text-sm text-secondary">
        Pending fix plans are approved step by step on the <Link className="underline" to="/infrastructure">Infrastructure</Link> page.
      </p>

      {status?.settings.diagnose_only_until_first_report && (
        <section data-testid="kube-first-run" className="space-y-2 rounded border border-line p-3">
          <p className="text-sm">The agent only diagnoses until you enable fix proposals.</p>
          <button className={primary} disabled={busy} onClick={() => act(() => kubeApi.enableProposals())}>
            Enable fix proposals
          </button>
        </section>
      )}

      <section data-testid="kube-issues" className="space-y-2">
        <h2 className="text-lg font-semibold text-primary">Current issues</h2>
        {current.length === 0 ? (
          <p className="text-sm text-secondary">No open issues.</p>
        ) : (
          <ul className="space-y-2">
            {current.map((i) => (
              <li key={i.key} data-testid={`kube-issue-${i.key}`} className="rounded border border-line p-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`rounded px-2 py-0.5 text-xs font-semibold ${MODE_CLASS[i.mode]}`}>{MODE_LABEL[i.mode]}</span>
                  <span className="font-medium">{i.reason}</span>
                  <span className="text-secondary">{i.namespace}/{i.name}</span>
                  {typeof i.evidence.restarts === 'number' && <span>{i.evidence.restarts} restarts</span>}
                  <span className="text-xs text-secondary">first seen {when(i.first_seen)}</span>
                  {i.mode === 'diagnose_only' && (
                    <button data-testid={`kube-override-${i.key}`} className={secondary} disabled={busy} onClick={() => override(i)}>
                      Override
                    </button>
                  )}
                </div>
                <div className="mt-1 text-xs text-secondary">
                  {Object.entries(i.evidence).filter(([, v]) => typeof v !== 'object').map(([k, v]) => `${k}=${String(v)}`).join(' · ')}
                </div>
                {i.override && (
                  <div className="mt-1 text-xs">Override by {i.override.created_by}, expires {when(i.override.expires_at)}</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section data-testid="kube-overrides" className="space-y-2">
        <h2 className="text-lg font-semibold text-primary">Active overrides</h2>
        {overrides.length === 0 ? (
          <p className="text-sm text-secondary">None.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {overrides.map((o) => (
              <li key={o.id} data-testid={`kube-override-row-${o.id}`} className="flex flex-wrap items-center gap-2">
                <span>{o.issue_key}</span>
                <span className="text-xs text-secondary">
                  by {o.created_by} at {when(o.created_at)}, expires {when(o.expires_at)}
                </span>
                <button className={secondary} disabled={busy} onClick={() => act(() => kubeApi.revoke(o.id))}>Revoke</button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section data-testid="kube-known-down" className="space-y-2">
        <h2 className="text-lg font-semibold text-primary">Known down</h2>
        {down.length === 0 ? (
          <p className="text-sm text-secondary">Nothing found in known-down namespaces.</p>
        ) : (
          <ul className="text-sm text-secondary">
            {down.map((i) => <li key={i.key}>{i.namespace}/{i.name}: {i.reason} (known down, not a failure)</li>)}
          </ul>
        )}
      </section>

      {draft && (
        <section data-testid="kube-settings" className="space-y-3">
          <h2 className="text-lg font-semibold text-primary">Settings</h2>
          <Chips label="Known down namespaces" testId="kube-known-down-list" value={draft.known_down}
            onChange={(v) => setDraft({ ...draft, known_down: v })} />
          <Chips label="Protected namespaces" testId="kube-protected-list" value={draft.protected_namespaces}
            onChange={(v) => setDraft({ ...draft, protected_namespaces: v })} />
          <div className="grid gap-2 sm:grid-cols-2">
            {NUMBER_FIELDS.map(([field, label]) => (
              <label key={field} className="flex items-center justify-between gap-2 text-sm">
                {label}
                <input
                  type="number"
                  className={`${input} w-24`}
                  value={draft[field] as number}
                  onChange={(e) => setDraft({ ...draft, [field]: Number(e.target.value) })}
                />
              </label>
            ))}
            <label className="flex items-center justify-between gap-2 text-sm">
              Prometheus URL
              <input
                className={`${input} w-64`}
                value={draft.prometheus_url ?? ''}
                onChange={(e) => setDraft({ ...draft, prometheus_url: e.target.value })}
                placeholder="http://prometheus:9090"
              />
            </label>
          </div>
          <button data-testid="kube-save-settings" className={primary} disabled={busy} onClick={saveSettings}>
            Save settings
          </button>
        </section>
      )}

      <section data-testid="kube-waste" className="space-y-2">
        <h2 className="text-lg font-semibold text-primary">Latest waste report (estimate)</h2>
        {!waste ? (
          <p className="text-sm text-secondary">No waste report yet. It runs once a day.</p>
        ) : (
          <div className="space-y-3">
            <p className="text-xs text-secondary">
              Collected {when(waste.created_at)}. Figures are estimates from a point-in-time read.
              {!waste.data.metrics_available && ' Metrics were not available.'}
            </p>
            <WasteBlock title="Idle pods" section={waste.data.idle_pods} />
            <WasteBlock title="Over-sized pods" section={waste.data.oversized_pods} />
            <WasteBlock title="Unused PVCs" section={waste.data.unused_pvcs} />
            <WasteBlock title="Orphaned PVs" section={waste.data.orphaned_pvs} />
            <WasteBlock title="Crash-loop burn" section={waste.data.crash_loop_burn} note="estimate" />
          </div>
        )}
      </section>
    </div>
  )
}
