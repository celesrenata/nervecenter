import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  omnirouteApi,
  type OmniRouteCheck,
  type OmniRouteKeyKind,
  type OmniRouteSettings,
  type OmniRouteTestResult,
} from '../api/omniroute'
import { ApiError } from '../api/client'

/**
 * Settings → OmniRoute. Two write-only keys (NS-ADR-118). The screen only
 * ever shows set / not set, where it comes from and when it last changed; the
 * typed value is cleared from the page as soon as it is saved.
 */
const KEYS: Array<{ kind: OmniRouteKeyKind; title: string; blurb: string }> = [
  {
    kind: 'regular',
    title: 'Regular key',
    blurb: 'Used for inference: Ask, agent runs, the route guard and the mail observer.',
  },
  {
    kind: 'privileged',
    title: 'Privileged (management) key',
    blurb:
      'Used only for the Skill Hub catalog and the read-only OmniRoute tools. Never used for inference.',
  },
]

const errText = (e: unknown): string =>
  e instanceof ApiError ? `${e.status} — ${e.detail}` : 'Request failed (network error)'

function describeCheck(label: string, c: OmniRouteCheck | undefined): string {
  if (!c) return ''
  if (!c.ok) return `${label}: ${c.status.replace(/_/g, ' ')}`
  const counts: string[] = []
  if (c.routes !== undefined) counts.push(`${c.routes} routes, ${c.ready ?? 0} ready`)
  if (c.skills !== undefined) counts.push(`${c.skills} skills`)
  return `${label}: ok${counts.length ? ` (${counts.join(', ')})` : ''}`
}

function KeyCard({
  kind,
  title,
  blurb,
  settings,
  onChanged,
}: {
  kind: OmniRouteKeyKind
  title: string
  blurb: string
  settings: OmniRouteSettings
  onChanged: (s: OmniRouteSettings) => void
}) {
  const status = settings.keys[kind]
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<OmniRouteTestResult | null>(null)

  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      await action()
    } catch (e) {
      setError(errText(e))
    } finally {
      setBusy(false)
    }
  }

  const save = (e: FormEvent) => {
    e.preventDefault()
    if (!value.trim()) return
    void run(async () => {
      const next = await omnirouteApi.setKey(kind, value.trim())
      setValue('') // the key never stays on the page
      setResult(null)
      setMessage('Key saved. It applies to the next request.')
      onChanged(next)
    })
  }

  const clear = () =>
    void run(async () => {
      const next = await omnirouteApi.clearKey(kind)
      setResult(null)
      setMessage('Stored key cleared. The cluster Secret value is used if one exists.')
      onChanged(next)
    })

  const test = () =>
    void run(async () => {
      setResult(await omnirouteApi.testKey(kind))
    })

  const sourceText =
    status.source === 'vault'
      ? 'stored in the encrypted vault'
      : status.source === 'env'
        ? 'from the cluster Secret'
        : ''

  return (
    <section
      data-testid={`omniroute-key-${kind}`}
      className="bg-elevated rounded-lg shadow-lg p-6 space-y-4"
    >
      <div>
        <h3 className="text-lg font-semibold text-primary">{title}</h3>
        <p className="text-sm text-muted">{blurb}</p>
      </div>
      <p data-testid={`omniroute-status-${kind}`} className="text-sm text-gray-700 dark:text-gray-300">
        <span className="font-medium">{status.configured ? 'Set' : 'Not set'}</span>
        {sourceText && ` — ${sourceText}`}
        {status.changed_at && ` · changed ${new Date(status.changed_at).toLocaleString()}`}
      </p>
      <form onSubmit={save} className="flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-[16rem]">
          <label
            htmlFor={`omniroute-input-${kind}`}
            className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
          >
            New {kind} key (write-only)
          </label>
          <input
            id={`omniroute-input-${kind}`}
            data-testid={`omniroute-input-${kind}`}
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={status.configured ? 'Enter a new key to replace it' : 'Paste the key'}
            className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
          />
        </div>
        <button
          type="submit"
          data-testid={`omniroute-save-${kind}`}
          disabled={busy || !value.trim() || !settings.vault_writable}
          className="bg-blue-500 hover:bg-blue-600 disabled:opacity-50 text-white px-4 py-2 rounded-lg"
        >
          Save key
        </button>
        <button
          type="button"
          data-testid={`omniroute-test-${kind}`}
          disabled={busy || !status.configured}
          onClick={test}
          className="border border-line px-4 py-2 rounded-lg text-sm disabled:opacity-50"
        >
          Test
        </button>
        {status.vault_set && (
          <button
            type="button"
            data-testid={`omniroute-clear-${kind}`}
            disabled={busy}
            onClick={clear}
            className="text-sm font-medium text-red-600 hover:underline disabled:opacity-50"
          >
            Clear stored key
          </button>
        )}
      </form>
      {!settings.vault_writable && (
        <p role="alert" className="text-sm text-amber-700 dark:text-amber-300">
          Keys cannot be stored here until the server has an encryption key (NERVE_SECRET_KEY).
        </p>
      )}
      {error && (
        <p role="alert" data-testid={`omniroute-error-${kind}`} className="text-sm text-red-600">
          {error}
        </p>
      )}
      {message && (
        <p role="status" data-testid={`omniroute-message-${kind}`} className="text-sm text-green-700 dark:text-green-300">
          {message}
        </p>
      )}
      {result && (
        <ul data-testid={`omniroute-result-${kind}`} className="text-sm space-y-1">
          {[
            describeCheck('Inference health', result.inference),
            describeCheck('Skill Hub catalog', result.catalog),
            describeCheck('OmniRoute MCP', result.mcp),
          ]
            .filter(Boolean)
            .map((line) => (
              <li key={line}>{line}</li>
            ))}
        </ul>
      )}
    </section>
  )
}

export function OmniRouteKeysPanel() {
  const [settings, setSettings] = useState<OmniRouteSettings | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setSettings(await omnirouteApi.get())
      setError(null)
    } catch (e) {
      setError(errText(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <section
      id="settings-panel-omniroute"
      role="tabpanel"
      className="space-y-6"
      aria-labelledby="settings-omniroute"
    >
      <div>
        <h2 id="settings-omniroute" className="text-xl font-semibold text-primary">
          OmniRoute
        </h2>
        <p className="text-sm text-muted max-w-2xl">
          Keys are stored encrypted on the server and are write-only: this screen never shows them.
          A key saved here overrides the cluster Secret; clearing it falls back to the Secret.
        </p>
      </div>
      {error && (
        <p role="alert" data-testid="omniroute-load-error" className="text-sm text-red-600">
          Could not load key status: {error}
        </p>
      )}
      {settings &&
        KEYS.map((k) => (
          <KeyCard key={k.kind} {...k} settings={settings} onChanged={setSettings} />
        ))}
      {settings && settings.audit.length > 0 && (
        <section className="bg-elevated rounded-lg shadow-lg p-6">
          <h3 className="text-lg font-semibold text-primary mb-2">Recent changes</h3>
          <ul data-testid="omniroute-audit" className="text-sm text-muted space-y-1">
            {settings.audit.map((a, i) => (
              <li key={`${a.at}-${i}`}>
                {a.at ? new Date(a.at).toLocaleString() : ''} · {a.actor} · {a.key} key · {a.action}{' '}
                · {a.outcome}
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  )
}
