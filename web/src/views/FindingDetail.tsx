import { Fragment, useCallback, useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { infraApi, type InfraConnector } from '../api/infra'
import {
  pentestApi,
  type FindingDetail as Detail,
  type FixSettings as Settings,
  type PentestFix,
} from '../api/pentest'

/**
 * One finding in full: what was found, why it matters, how to fix it, and, where NerveCenter
 * can do it safely, the controlled fix (NS-ADR-128). Applying changes the production firewall,
 * so it is always preview first, then an explicit confirmation.
 */
const errText = (e: unknown) => (e instanceof ApiError ? e.detail : 'Request failed')
const btn = 'rounded px-3 py-1.5 text-sm font-medium disabled:opacity-50'
const primary = `${btn} bg-blue-600 text-white hover:bg-blue-700`
const danger = `${btn} bg-red-700 text-white hover:bg-red-800`
const secondary = `${btn} border border-line text-primary hover:bg-hover`

const STATUS_TEXT: Record<string, string> = {
  previewed: 'Previewed. Nothing has changed yet.',
  applying: 'Applying…',
  succeeded: 'Fixed and verified',
  failed_restored: 'Failed. The firewall was restored and verified.',
  failed_unrestored: 'Failed. Needs your attention.',
  rolled_back: 'Rolled back',
  archived: 'Fixed (rollback window ended)',
  cancelled: 'Cancelled',
}
const STATUS_CLASS: Record<string, string> = {
  succeeded: 'border-green-600 bg-green-600/10',
  archived: 'border-green-600 bg-green-600/10',
  failed_restored: 'border-amber-500 bg-amber-500/10',
  failed_unrestored: 'border-red-600 bg-red-600/10',
  rolled_back: 'border-line bg-hover',
  previewed: 'border-blue-600 bg-blue-600/10',
  applying: 'border-blue-600 bg-blue-600/10',
}
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '—')
const revTime = (t: unknown) => {
  const n = Number(t)
  return Number.isFinite(n) && n > 0 ? `${n} (${new Date(n * 1000).toLocaleString()})` : '—'
}

function Revision({ title, rev }: { title: string; rev: PentestFix['revision_before'] }) {
  if (!rev || rev.time === undefined) return null
  return (
    <div className="text-xs">
      <div className="font-semibold text-primary">{title}</div>
      <div>revision {revTime(rev.time)}</div>
      {rev.description && <div className="text-secondary">{rev.description} {rev.username ? `(${rev.username})` : ''}</div>}
      {rev.sha256 && <div className="break-all font-mono text-secondary">sha256 {rev.sha256}</div>}
    </div>
  )
}

function ManualRestore({ fix }: { fix: PentestFix }) {
  if (fix.manual_restore.length === 0) return null
  return (
    <details data-testid="fix-manual-restore" className="text-xs" open={fix.status === 'failed_unrestored'}>
      <summary className="cursor-pointer font-semibold text-primary">Restore it yourself</summary>
      <ol className="ml-4 list-decimal space-y-1 pt-1">
        {fix.manual_restore.map((line) => (
          <li key={line} className="break-all font-mono">{line}</li>
        ))}
      </ol>
    </details>
  )
}

function FixCard({ fix, busy, onApply, onRollback }: {
  fix: PentestFix
  busy: boolean
  onApply: (id: string) => void
  onRollback: (id: string) => void
}) {
  const [understood, setUnderstood] = useState(false)
  return (
    <div data-testid={`fix-${fix.id}`} className={`space-y-2 rounded border p-3 text-sm ${STATUS_CLASS[fix.status] ?? 'border-line'}`}>
      <div className="font-semibold text-primary" data-testid="fix-status">{STATUS_TEXT[fix.status] ?? fix.status}</div>
      {fix.error && <p role="alert" className="text-red-700">{fix.error}</p>}
      <div className="grid gap-3 sm:grid-cols-2">
        <Revision title="Firewall revision before" rev={fix.revision_before} />
        <Revision title="Firewall revision after" rev={fix.revision_after} />
      </div>
      {fix.backup.path && (
        <div className="break-all text-xs">Backup on the firewall: <span className="font-mono">{fix.backup.path}</span></div>
      )}
      {fix.status === 'previewed' && (
        <div className="space-y-2">
          {fix.preview.rule && (
            <div className="text-xs">
              Rule <span className="font-mono">{fix.tracker}</span>: {fix.preview.rule.type} on {fix.preview.rule.interface}
              {fix.preview.rule.descr ? ` “${fix.preview.rule.descr}”` : ''}. {fix.preview.effect}
            </div>
          )}
          <ol className="ml-4 list-decimal text-xs">
            {(fix.preview.plan ?? []).map((p) => <li key={p}>{p}</li>)}
          </ol>
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
            <span>
              I understand this changes the production firewall. The firewall restores its backup by
              itself after {fix.preview.watchdog_seconds ?? 300} s unless the change is verified.
            </span>
          </label>
          <button className={danger} disabled={busy || !understood} onClick={() => onApply(fix.id)}>
            Apply fix to the firewall
          </button>
        </div>
      )}
      {fix.checks.length > 0 && (
        <ul data-testid="fix-checks" className="space-y-0.5 text-xs">
          {fix.checks.map((c) => (
            <li key={c.name}>{c.ok ? '✓' : '✗'} {c.name} <span className="text-secondary">{c.detail}</span></li>
          ))}
        </ul>
      )}
      {fix.status === 'succeeded' && (
        <div className="space-y-1">
          <div className="text-xs">Rollback is available until {when(fix.rollback_until)}, then the fix is archived.</div>
          {fix.can_rollback && (
            <button className={secondary} disabled={busy} onClick={() => onRollback(fix.id)}>Roll back this fix</button>
          )}
        </div>
      )}
      {fix.log.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer font-semibold text-primary">Step log</summary>
          <ul className="space-y-0.5 pt-1">
            {fix.log.map((l, i) => (
              <li key={`${l.at}-${i}`}>{l.ok ? '✓' : '✗'} {l.step}{l.detail ? ` — ${l.detail}` : ''}</li>
            ))}
          </ul>
        </details>
      )}
      <ManualRestore fix={fix} />
    </div>
  )
}

export function FindingDetail({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    try {
      setDetail(await pentestApi.finding(id))
      setError(null)
    } catch (e) {
      setError(errText(e))
    }
  }, [id])
  useEffect(() => { void load() }, [load])
  // While a fix runs on the server, keep the card fresh.
  const running = detail?.fixes.some((f) => f.status === 'applying') ?? false
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => { void load() }, 2000)
    return () => clearInterval(t)
  }, [running, load])

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await fn()
      setError(null)
    } catch (e) {
      setError(errText(e))
    } finally {
      await load()
      onChanged()
      setBusy(false)
    }
  }
  if (!detail) {
    return <div className="rounded border border-line p-3 text-sm">{error ?? 'Loading…'}</div>
  }
  const { finding: f, advice: a, fixes } = detail
  const extra = Object.entries(f.details ?? {})
  return (
    <section data-testid="finding-detail" className="space-y-3 rounded border border-line p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold text-primary">{f.title}</h2>
          <div className="text-xs text-secondary">{f.subject} · {f.kind} / {f.rule || '—'} · {f.severity} · {f.status}</div>
        </div>
        <button className={secondary} onClick={onClose}>Close</button>
      </div>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}

      <div>
        <h3 className="text-sm font-semibold text-primary">What was found</h3>
        <p className="whitespace-pre-wrap text-sm">{f.evidence || '—'}</p>
        <div className="text-xs text-secondary">
          First seen {when(f.created_at)} · last seen {when(f.last_seen_at)}
          {f.advisory_id ? ` · ${f.advisory_id}` : ''}
          {f.labels.length > 0 ? ` · ${f.labels.join(', ')}` : ''}
        </div>
        {extra.length > 0 && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 text-xs">
            {extra.map(([k, v]) => (
              <Fragment key={k}><dt className="text-secondary">{k}</dt><dd>{String(v)}</dd></Fragment>
            ))}
          </dl>
        )}
        {f.sources.length > 0 && <div className="text-xs text-secondary">Sources: {f.sources.join(' · ')}</div>}
      </div>

      <div>
        <h3 className="text-sm font-semibold text-primary">Why it matters</h3>
        <p className="text-sm">{a.why}</p>
      </div>

      <div>
        <h3 className="text-sm font-semibold text-primary">Proposed fix</h3>
        <ol data-testid="fix-steps" className="ml-5 list-decimal space-y-0.5 text-sm">
          {a.steps.map((s) => <li key={s}>{s}</li>)}
        </ol>
        <p className="pt-1 text-xs text-secondary">To confirm: {a.verify.join(' ')}</p>
      </div>

      <div data-testid="fix-automation" className="space-y-2 rounded bg-hover p-3">
        {a.automation.available ? (
          <>
            <div className="text-sm font-semibold text-primary">NerveCenter can apply this fix</div>
            <p className="text-sm">{a.automation.effect}</p>
            <p className="text-xs text-secondary">
              It backs up the firewall configuration first, applies the change, checks the result
              and what else it might have affected, and restores the backup by itself if anything
              looks wrong. You can roll it back for 7 days.
            </p>
            {!fixes.some((x) => x.status === 'previewed' || x.status === 'applying') && f.status === 'open' && (
              <button className={primary} disabled={busy} onClick={() => act(() => pentestApi.previewFix(f.id))}>
                Preview the change
              </button>
            )}
          </>
        ) : (
          <>
            <div className="text-sm font-semibold text-primary">This one needs you</div>
            <p className="text-sm">{a.automation.reason}</p>
          </>
        )}
      </div>

      {fixes.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-primary">Fix attempts</h3>
          {fixes.map((fx) => (
            <FixCard
              key={fx.id}
              fix={fx}
              busy={busy}
              onApply={(fid) => act(() => pentestApi.applyFix(fid))}
              onRollback={(fid) => act(() => pentestApi.rollbackFix(fid))}
            />
          ))}
        </div>
      )}
    </section>
  )
}

/** Which SSH connector reaches pfSense, and how its login menu opens a shell. */
export function FixSettings() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [connectors, setConnectors] = useState<InfraConnector[]>([])
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    void (async () => {
      try {
        const [s, c] = await Promise.all([pentestApi.fixSettings(), infraApi.connectors()])
        setSettings(s)
        setConnectors(c.filter((x) => x.kind === 'ssh'))
      } catch (e) {
        setMessage(errText(e))
      }
    })()
  }, [])
  if (!settings) return message ? <p className="text-sm text-red-600">{message}</p> : null
  const run = async (fn: () => Promise<string>) => {
    setBusy(true)
    try {
      setMessage(await fn())
    } catch (e) {
      setMessage(errText(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section data-testid="pentest-fix-settings" className="space-y-2">
      <h2 className="text-lg font-semibold text-primary">Applying fixes to pfSense</h2>
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="flex flex-col gap-1">SSH connector
          <select value={settings.fix_connector_id ?? ''} className="rounded border border-line bg-transparent p-1"
            onChange={(e) => setSettings({ ...settings, fix_connector_id: e.target.value || null })}>
            <option value="">None (fixes disabled)</option>
            {connectors.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.host})</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">Menu option that opens a shell
          <input value={settings.fix_menu_option} maxLength={2} className="w-20 rounded border border-line bg-transparent p-1"
            onChange={(e) => setSettings({ ...settings, fix_menu_option: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">Self-restore after (seconds)
          <input type="number" min={180} max={3600} value={settings.fix_watchdog_seconds}
            className="w-28 rounded border border-line bg-transparent p-1"
            onChange={(e) => setSettings({ ...settings, fix_watchdog_seconds: Number(e.target.value) })} />
        </label>
        <button className={secondary} disabled={busy}
          onClick={() => run(async () => { setSettings(await pentestApi.saveFixSettings(settings)); return 'Saved.' })}>
          Save
        </button>
        <button className={secondary} disabled={busy || !settings.fix_connector_id}
          onClick={() => run(async () => {
            const r = await pentestApi.testFixConnection()
            return `Connected. pfSense config version ${r.version ?? '?'}, revision ${revTime(r.revision?.time)}. Nothing was changed.`
          })}>
          Test connection (read-only)
        </button>
      </div>
      <p className="text-xs text-secondary">
        Leave the menu option empty if the account logs straight into a shell. The default 8 is
        pfSense&apos;s &ldquo;Shell&rdquo; entry.
      </p>
      {message && <p role="status" className="text-sm">{message}</p>}
    </section>
  )
}
