import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import { infraApi, type InfraConnector, type InfraKind, type InfraPlan, type InfraStep } from '../api/infra'

/**
 * Infrastructure (NS-ADR-120): clusters and SSH hosts agents may inspect
 * (read-only), and the change plans they propose. Nothing changes until a plan
 * is approved here; credentials are write-only and never shown.
 */
const errText = (e: unknown) => (e instanceof ApiError ? `${e.status} — ${e.detail}` : 'Request failed')
const input = 'w-full rounded border border-line bg-surface px-2 py-1.5 text-sm'
const btn = 'rounded px-3 py-1.5 text-sm font-medium disabled:opacity-50'
const primary = `${btn} bg-blue-600 text-white hover:bg-blue-700`
const secondary = `${btn} border border-line text-primary hover:bg-hover`

/** Plain-language text for the executor's refusal codes. */
const REASONS: Record<string, string> = {
  host_key_not_pinned: 'The host key is not confirmed yet: press "Scan host key", check the fingerprint, then "Trust these keys".',
  ssh_key_missing: 'No SSH key is stored yet: paste the private key and press "Save credential".',
  kube_token_missing: 'No token is stored yet: paste it and press "Save credential".',
  host_unreachable: 'The host did not answer on that port.',
  binary_missing: 'The executor is missing kubectl or ssh.',
  unknown_connector: 'That connector no longer exists.',
}
const reasonText = (code?: string) => (code ? REASONS[code] ?? code : 'unknown')

function stepText(s: InfraStep): string {
  if (s.args) return `kubectl ${s.args.join(' ')}`
  const params = s.params && Object.keys(s.params).length ? ` ${JSON.stringify(s.params)}` : ''
  return `ssh: ${s.command ?? ''}${params}`
}

function AddConnector({ onAdded }: { onAdded: () => void }) {
  const [kind, setKind] = useState<InfraKind>('kubernetes')
  const [name, setName] = useState('')
  const [server, setServer] = useState('https://10.1.1.12:6443')
  const [namespace, setNamespace] = useState('')
  const [ca, setCa] = useState('')
  const [insecure, setInsecure] = useState(false)
  const [host, setHost] = useState('')
  const [port, setPort] = useState(22)
  const [user, setUser] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const config =
        kind === 'kubernetes'
          ? { server, namespace: namespace || undefined, ca_pem: ca || undefined, insecure_skip_tls_verify: insecure }
          : { host, port, user }
      const created = await infraApi.create(kind, name, config)
      if (kind === 'ssh') {
        // Fetch the host key straight away so its fingerprint is ready to confirm.
        await infraApi.scanHostKey(created.name).catch(() => undefined)
      }
      setName('')
      onAdded()
    } catch (err) {
      setError(errText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} data-testid="infra-add" className="space-y-3 rounded-lg border border-line p-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="text-sm">
          <span className="text-muted">Type</span>
          <select className={input} value={kind} onChange={(e) => setKind(e.target.value as InfraKind)}>
            <option value="kubernetes">Kubernetes cluster</option>
            <option value="ssh">SSH host</option>
          </select>
        </label>
        <label className="text-sm">
          <span className="text-muted">Name</span>
          <input className={input} value={name} onChange={(e) => setName(e.target.value)} placeholder={kind === 'ssh' ? 'esnixi' : 'home-cluster'} required />
        </label>
      </div>
      {kind === 'kubernetes' ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm">
            <span className="text-muted">API server</span>
            <input className={input} value={server} onChange={(e) => setServer(e.target.value)} required />
          </label>
          <label className="text-sm">
            <span className="text-muted">Default namespace (optional)</span>
            <input className={input} value={namespace} onChange={(e) => setNamespace(e.target.value)} />
          </label>
          <label className="text-sm sm:col-span-2">
            <span className="text-muted">CA certificate (PEM, optional)</span>
            <textarea className={`${input} font-mono`} rows={3} value={ca} onChange={(e) => setCa(e.target.value)} />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={insecure} disabled={!!ca} onChange={(e) => setInsecure(e.target.checked)} />
            Skip TLS verification (only without a CA)
          </label>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="text-sm">
            <span className="text-muted">Host</span>
            <input className={input} value={host} onChange={(e) => setHost(e.target.value)} placeholder="192.168.42.254" required />
          </label>
          <label className="text-sm">
            <span className="text-muted">Port</span>
            <input className={input} type="number" value={port} onChange={(e) => setPort(Number(e.target.value))} />
          </label>
          <label className="text-sm">
            <span className="text-muted">User</span>
            <input className={input} value={user} onChange={(e) => setUser(e.target.value)} required />
          </label>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <button type="submit" disabled={busy} className={primary} data-testid="infra-add-submit">
        {busy ? 'Adding…' : 'Add connector'}
      </button>
    </form>
  )
}

function ConnectorCard({ c, onChange }: { c: InfraConnector; onChange: () => void }) {
  const [token, setToken] = useState('')
  const [key, setKey] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [note, setNote] = useState<string | null>(null)
  const [output, setOutput] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true)
    setNote(null)
    try {
      await fn()
      if (ok) setNote(ok)
      onChange()
    } catch (err) {
      setNote(errText(err))
    } finally {
      setBusy(false)
    }
  }

  const saveSecret = (e: FormEvent) => {
    e.preventDefault()
    const secret: Record<string, string> =
      c.kind === 'kubernetes' ? { token } : passphrase ? { private_key: key, passphrase } : { private_key: key }
    void run(async () => {
      await infraApi.setSecret(c.name, secret)
      setToken('')
      setKey('')
      setPassphrase('')
    }, 'Saved to the encrypted vault.')
  }

  const needsHostKey = c.kind === 'ssh' && c.host_key_status !== 'pinned'
  const test = () =>
    void run(async () => {
      const r = await infraApi.test(c.name)
      setOutput(r.ok ? r.output ?? '' : `Refused or failed: ${r.denied_reason ? reasonText(r.denied_reason) : r.output ?? 'unknown'}`)
    })

  return (
    <li className="space-y-3 rounded-lg border border-line p-4" data-testid={`infra-connector-${c.name}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-medium text-primary">
            {c.name} <span className="ml-1 rounded bg-surface px-1.5 py-0.5 text-xs uppercase text-muted">{c.kind}</span>
          </p>
          <p className="text-xs text-muted">
            {c.kind === 'kubernetes' ? `${c.server}${c.namespace ? ` · ns ${c.namespace}` : ''}` : `${c.user}@${c.host}:${c.port}`}
            {' · '}
            {c.secret_set ? 'credential set' : 'no credential yet'}
            {c.kind === 'ssh' && ` · host key ${c.host_key_status}`}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            className={secondary}
            disabled={busy || !c.secret_set || needsHostKey}
            title={needsHostKey ? 'Confirm the host key first' : !c.secret_set ? 'Save a credential first' : undefined}
            onClick={test}
            data-testid={`infra-test-${c.name}`}
          >
            Test
          </button>
          {c.kind === 'ssh' && (
            <button className={secondary} disabled={busy} onClick={() => void run(() => infraApi.scanHostKey(c.name))} data-testid={`infra-scan-${c.name}`}>
              Scan host key
            </button>
          )}
          <button
            className={`${btn} border border-red-300 text-red-700`}
            disabled={busy}
            onClick={() => window.confirm(`Delete ${c.name} and its stored credential?`) && void run(() => infraApi.remove(c.name))}
          >
            Delete
          </button>
        </div>
      </div>

      {c.kind === 'ssh' && c.host_key_status === 'pending' && (c.pending_fingerprints?.length ?? 0) > 0 && (
        <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm dark:bg-amber-900/20" data-testid={`infra-pending-${c.name}`}>
          <p>Check these fingerprints against the host (for example <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code> on it):</p>
          <ul className="my-2 font-mono text-xs">{c.pending_fingerprints!.map((f) => <li key={f}>{f}</li>)}</ul>
          <button className={primary} disabled={busy} onClick={() => void run(() => infraApi.trustHostKey(c.name, c.pending_fingerprints!), 'Host key pinned.')}>
            Trust these keys
          </button>
        </div>
      )}
      {needsHostKey && c.host_key_status !== 'pending' && (
        <p className="text-sm text-amber-700 dark:text-amber-300" data-testid={`infra-needs-key-${c.name}`}>
          Before this host can be used: press <strong>Scan host key</strong>, check the fingerprint, then <strong>Trust these keys</strong>.
        </p>
      )}
      {c.kind === 'ssh' && c.host_key_status === 'pinned' && (
        <p className="font-mono text-xs text-muted">Pinned: {(c.fingerprints ?? []).join(', ')}</p>
      )}

      <form onSubmit={saveSecret} className="space-y-2">
        {c.kind === 'kubernetes' ? (
          <input className={input} type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)}
            placeholder={c.secret_set ? 'Replace the bearer token' : 'Bearer token'} data-testid={`infra-token-${c.name}`} />
        ) : (
          <>
            <textarea className={`${input} font-mono`} rows={3} autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
              placeholder={c.secret_set ? 'Replace the private key (RSA or Ed25519)' : 'Paste the private key (RSA or Ed25519)'} data-testid={`infra-key-${c.name}`} />
            <input className={input} type="password" autoComplete="off" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} placeholder="Passphrase (if the key has one)" />
          </>
        )}
        <button type="submit" className={secondary} disabled={busy || (c.kind === 'kubernetes' ? !token : !key)}>Save credential</button>
      </form>
      {note && <p className="text-sm text-muted" role="status">{note}</p>}
      {output !== null && <pre className="max-h-64 overflow-auto rounded bg-surface p-2 text-xs" data-testid={`infra-output-${c.name}`}>{output}</pre>}
    </li>
  )
}

function PlanCard({ p, onChange }: { p: InfraPlan; onChange: () => void }) {
  const [mode, setMode] = useState<'opencode' | 'direct'>('opencode')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [verify, setVerify] = useState<string | null>(null)
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      onChange()
    } catch (err) {
      setError(errText(err))
    } finally {
      setBusy(false)
    }
  }
  return (
    <li className="space-y-2 rounded-lg border border-line p-4" data-testid={`infra-plan-${p.id}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-medium text-primary">{p.title}</p>
        <span className="rounded bg-surface px-2 py-0.5 text-xs uppercase text-muted">{p.status}</span>
      </div>
      <p className="text-xs text-muted">Proposed by {p.agent_id ?? 'unknown'}{p.decided_by ? ` · decided by ${p.decided_by}` : ''}</p>
      {p.summary && <p className="whitespace-pre-wrap text-sm">{p.summary}</p>}
      <ol className="list-decimal space-y-1 pl-5 text-sm">
        {p.steps.map((s, i) => {
          const r = p.results.find((x) => x.index === i)
          return (
            <li key={i}>
              <span className="font-mono text-xs">[{s.connector}] {stepText(s)}</span>
              {s.purpose && <span className="text-muted"> — {s.purpose}</span>}
              {r && (
                <pre className={`mt-1 max-h-48 overflow-auto rounded p-2 text-xs ${r.ok ? 'bg-surface' : 'bg-red-50 dark:bg-red-900/20'}`}>
                  {r.ok ? 'ok' : `failed (exit ${r.exit ?? '-'})`}{'\n'}{r.output}
                </pre>
              )}
            </li>
          )
        })}
      </ol>
      {p.verify.length > 0 && <p className="text-xs text-muted">Verify: {p.verify.map(stepText).join(' · ')}</p>}
      {p.rollback && <p className="text-xs text-muted">Rollback: {p.rollback}</p>}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {p.status === 'proposed' && (
        <div className="flex flex-wrap items-center gap-2">
          <select className={`${input} w-auto`} value={mode} onChange={(e) => setMode(e.target.value as 'opencode' | 'direct')}>
            <option value="opencode">Act through opencode (stops on a surprise)</option>
            <option value="direct">Run the steps directly</option>
          </select>
          <button className={primary} disabled={busy} onClick={() => void act(() => infraApi.approve(p.id, mode))} data-testid={`infra-approve-${p.id}`}>Approve</button>
          <button className={secondary} disabled={busy} onClick={() => void act(() => infraApi.reject(p.id))}>Reject</button>
        </div>
      )}
      {(p.status === 'done' || p.status === 'failed') && p.verify.length > 0 && (
        <button className={secondary} disabled={busy} onClick={() => void act(async () => {
          const r = await infraApi.verify(p.id)
          setVerify(r.verify.map((v) => `${v.ok ? 'ok' : 'failed'}: ${v.output ?? v.denied_reason ?? ''}`).join('\n\n'))
        })}>Run verification</button>
      )}
      {verify && <pre className="max-h-48 overflow-auto rounded bg-surface p-2 text-xs">{verify}</pre>}
    </li>
  )
}

export function InfrastructureView() {
  const [connectors, setConnectors] = useState<InfraConnector[]>([])
  const [plans, setPlans] = useState<InfraPlan[]>([])
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)

  const load = useCallback(async () => {
    try {
      const [c, p] = await Promise.all([infraApi.connectors(), infraApi.plans()])
      setConnectors(c)
      setPlans(p)
      setError(null)
    } catch (err) {
      setError(errText(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const pending = plans.filter((p) => p.status === 'proposed')
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-primary">Infrastructure</h1>
        <button className={primary} onClick={() => setAdding((v) => !v)} data-testid="infra-add-toggle">
          {adding ? 'Close' : '+ Add connector'}
        </button>
      </div>
      <p className="max-w-3xl text-sm text-muted">
        Agents read clusters and hosts here with read-only commands (no Kubernetes Secrets). To change
        anything they propose a plan; nothing runs until you approve it below. Tokens and keys are stored
        encrypted and never shown again.
      </p>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {adding && <AddConnector onAdded={() => { setAdding(false); void load() }} />}

      <section className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Plans waiting for you ({pending.length})</h2>
        {plans.length === 0 ? (
          <p className="text-sm text-muted">No plans yet.</p>
        ) : (
          <ul className="space-y-3">{plans.map((p) => <PlanCard key={p.id} p={p} onChange={() => void load()} />)}</ul>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Connectors</h2>
        {connectors.length === 0 ? (
          <p className="text-sm text-muted">No clusters or hosts yet. Add one to let agents look at it.</p>
        ) : (
          <ul className="space-y-3">{connectors.map((c) => <ConnectorCard key={c.id} c={c} onChange={() => void load()} />)}</ul>
        )}
      </section>
    </div>
  )
}
