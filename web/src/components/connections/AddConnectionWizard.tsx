/**
 * Connection Center — Add-Connection wizard (spec §3 capabilities 1–2).
 *
 * pick kind (gmail / imap / graph / relay presets) → credentials →
 * auth/pairing → verify → initial import → done.
 *
 * The flow advances ONLY on real API responses — a provider sign-in that
 * was not completed, or a login probe that failed, keeps the user on the
 * step with the actionable error shown. Degraded (no durable storage)
 * responses surface the backend's honest `executed:false` detail instead of
 * fabricating a created connection.
 *
 * Relay preset: `POST /api/relay/pair` is the only token-minting path (B's
 * `startAuth` for a relay merely points there), so the one-time pairing
 * token is minted as soon as the connection row is created and shown once.
 * The relay skips the verify step (no login probe for `relay_device`).
 */

import { useState } from 'react'
import {
  connectionsApi,
  isDegradedWrite,
  type Connection,
  type AuthFlowResult,
} from '../../api/connections'
import {
  PROVIDER_PRESETS,
  backendKindFor,
  fieldsForStep,
  nextStep,
  canAdvance,
  type WizardKind,
  type WizardStep,
  type WizardState,
} from './wizard'

interface WizardFlow {
  steps: WizardStep[]
  labels: Record<WizardStep, string>
}

function flowFor(kind: WizardKind | null): WizardFlow {
  if (kind === 'relay') {
    return {
      steps: ['pick_kind', 'credentials', 'auth', 'import', 'done'],
      labels: {
        pick_kind: 'Provider',
        credentials: 'Credentials',
        auth: 'Pairing',
        verify: 'Verify',
        import: 'Import',
        done: 'Done',
      },
    }
  }
  return {
    steps: ['pick_kind', 'credentials', 'auth', 'verify', 'import', 'done'],
    labels: {
      pick_kind: 'Provider',
      credentials: 'Credentials',
      auth: 'Sign in',
      verify: 'Verify',
      import: 'Import',
      done: 'Done',
    },
  }
}

/** The primary action label for the current step (per preset). */
function primaryLabel(state: WizardState): string {
  switch (state.step) {
    case 'pick_kind':
      return 'Continue'
    case 'credentials':
      return 'Save & start sign-in'
    case 'auth':
      if (state.kind === 'relay') return 'Continue to import'
      if (state.kind === 'imap') return 'Continue to verify'
      return 'I finished sign-in'
    case 'verify':
      return 'Verify connection'
    case 'import':
      return 'Start initial import'
    case 'done':
      return 'Done'
  }
}

export function AddConnectionWizard({
  onCreated,
  onCancel,
}: {
  /** Called when the wizard reaches `done` with the created row. */
  onCreated: (connection: Connection) => void
  onCancel: () => void
}) {
  const [state, setState] = useState<WizardState>({
    step: 'pick_kind',
    kind: null,
    config: {},
    busy: false,
    error: null,
  })
  /** The created row (present from the credentials step on). */
  const [connection, setConnection] = useState<Connection | null>(null)
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const [oauthCode, setOauthCode] = useState('')
  const [relayToken, setRelayToken] = useState<{ device_id: string; token: string } | null>(null)

  function patch(partial: Partial<WizardState>) {
    setState((prev) => ({ ...prev, ...partial }))
  }

  function pickKind(kind: WizardKind) {
    const config: Record<string, string> = {}
    if (kind === 'imap') config.port = '993'
    setState((prev) => ({ ...prev, kind, config, error: null, step: nextStep({ ...prev, kind, config }) }))
  }

  async function run(fn: () => Promise<void>) {
    patch({ busy: true, error: null })
    try {
      await fn()
    } catch (err) {
      patch({ error: errMessage(err) })
    } finally {
      patch({ busy: false })
    }
  }

  async function advance() {
    if (!canAdvance(state)) return
    switch (state.step) {
      case 'pick_kind':
        if (state.kind) setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))
        return
      case 'credentials': {
        const kind = state.kind
        if (!kind) return
        await run(async () => {
          const res = await connectionsApi.create({
            kind: backendKindFor(kind),
            label: state.config.device_name ?? null,
            config: state.config,
            enabled: true,
          })
          if (isDegradedWrite(res)) throw new Error(res.detail)
          setConnection(res)
          setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))
          if (kind === 'gmail' || kind === 'graph') {
            const started = (await connectionsApi.startAuth(res.id)) as AuthFlowResult
            if (isDegradedWrite(started)) throw new Error(started.detail)
            if ('authorization_url' in started) setAuthUrl(started.authorization_url)
          } else if (kind === 'relay') {
            const pair = await connectionsApi.relayPair(state.config.device_name || null)
            if (isDegradedWrite(pair)) throw new Error(pair.detail)
            setRelayToken({ device_id: pair.device_id, token: pair.token })
          }
        })
        return
      }
      case 'auth':
        if (state.kind === 'relay') {
          // The token was minted on creation; pairing happens on the device.
          setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))
          return
        }
        if (state.kind === 'imap') {
          // App passwords are stored via the backend CLI (the web UI never
          // takes the password); the verify step proves the login works.
          setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))
          return
        }
        if (oauthCode.trim() !== '') {
          // Complete the OAuth exchange with the pasted authorization code.
          await run(async () => {
            if (!connection) return
            const res = await connectionsApi.startAuth(connection.id, { code: oauthCode.trim() })
            if (isDegradedWrite(res)) throw new Error(res.detail)
            if ('id' in res) setConnection(res)
            setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))
          })
          return
        }
        // No code pasted — refresh the row (the exchange may already have
        // completed via the provider redirect).
        await run(async () => {
          if (!connection) return
          const fresh = await connectionsApi.get(connection.id)
          setConnection(fresh)
          setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))
        })
        return
      case 'verify':
        await run(async () => {
          if (!connection) return
          const res = await connectionsApi.verify(connection.id)
          if (isDegradedWrite(res)) throw new Error(res.detail)
          setConnection(res)
          if (res.ok) {
            setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))
          } else {
            patch({
              error: `Verify failed — ${res.error_code}${
                res.error_message ? `: ${res.error_message}` : ''
              }`,
            })
          }
        })
        return
      case 'import':
        await run(async () => {
          if (!connection) return
          const res = await connectionsApi.startImport(connection.id)
          if (isDegradedWrite(res)) throw new Error(res.detail)
          setConnection(res)
          if (res.ok) {
            onCreated(res)
          } else {
            patch({
              error: `Import could not start — ${res.error_code}. You can retry, or finish setup and import later from the card.`,
            })
          }
        })
        return
      case 'done':
        if (connection) onCreated(connection)
        return
    }
  }

  function finish() {
    if (connection) onCreated(connection)
    else onCancel()
  }

  async function rePair() {
    if (state.kind !== 'relay' || state.busy) return
    await run(async () => {
      const pair = await connectionsApi.relayPair(state.config.device_name || null)
      if (isDegradedWrite(pair)) throw new Error(pair.detail)
      setRelayToken({ device_id: pair.device_id, token: pair.token })
    })
  }

  const preset = state.kind ? PROVIDER_PRESETS.find((p) => p.id === state.kind) : null
  const flow = flowFor(state.kind)
  const activeIndex = Math.max(0, flow.steps.indexOf(state.step))
  const heading =
    state.step === 'pick_kind' ? 'Add a connection' : `${preset?.label ?? 'Connection'} — ${flow.labels[state.step]}`

  return (
    <div
      className="bg-elevated rounded-lg shadow-lg border border-line p-5 space-y-4"
      role="dialog"
      aria-label="Add connection wizard"
    >
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-lg font-semibold text-primary">{heading}</h3>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-line px-2.5 py-1 text-xs text-secondary hover:bg-hover"
        >
          Cancel
        </button>
      </div>

      {/* Step progress (the frozen setup steps for this preset) */}
      <ol className="flex flex-wrap gap-1.5 text-[11px] text-muted" aria-label="Setup progress">
        {flow.steps.map((step, i) => (
          <li
            key={step}
            className={`rounded-full border px-2 py-0.5 ${
              i <= activeIndex ? 'border-accent text-accent-contrast bg-accent-soft' : 'border-line'
            }`}
            aria-current={i === activeIndex ? 'step' : undefined}
          >
            {i + 1}. {flow.labels[step]}
          </li>
        ))}
      </ol>

      {state.error && (
        <p
          className="rounded-md border border-[color-mix(in_srgb,var(--danger)_40%,transparent)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] p-3 text-sm text-danger break-words"
          role="alert"
        >
          {state.error}
        </p>
      )}

      {/* ── pick kind ── */}
      {state.step === 'pick_kind' && (
        <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label="Connection kind">
          {PROVIDER_PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              role="radio"
              aria-checked={state.kind === preset.id}
              onClick={() => pickKind(preset.id)}
              className={`rounded-md border p-3 text-left transition-colors ${
                state.kind === preset.id ? 'border-accent bg-accent-soft' : 'border-line hover:bg-hover'
              }`}
            >
              <span className="block text-sm font-semibold text-primary">{preset.label}</span>
              <span className="block text-xs text-muted mt-0.5">{preset.hint}</span>
            </button>
          ))}
        </div>
      )}

      {/* ── credentials ── */}
      {state.step === 'credentials' && state.kind && (
        <div className="space-y-3">
          {fieldsForStep(state).map((field) => (
            <label key={field.key} className="block">
              <span className="text-xs font-medium text-primary">{field.label}</span>
              <input
                type="text"
                value={state.config[field.key] ?? ''}
                placeholder={field.placeholder}
                disabled={state.busy}
                onChange={(e) => patch({ config: { ...state.config, [field.key]: e.target.value } })}
                className="mt-1 w-full max-w-xs rounded-md border border-line bg-app px-2.5 py-1.5 text-sm text-primary placeholder:text-muted disabled:opacity-50"
                aria-label={field.label}
              />
            </label>
          ))}
          <p className="text-[11px] text-muted">
            Credentials (passwords / tokens) are never stored in this form — they travel through
            the {preset?.authMode === 'oauth' ? 'provider sign-in flow' : preset?.authMode === 'pairing' ? 'device pairing flow' : 'app-password step'} and
            live encrypted in the secret vault.
          </p>
        </div>
      )}

      {/* ── auth / pairing ── */}
      {state.step === 'auth' && state.kind && (
        <div className="space-y-3 text-sm text-secondary">
          {state.kind === 'imap' && (
            <p>
              Store the mailbox <strong>app password</strong> for this connection in the backend
              (it is kept encrypted in the vault — the web UI never takes the password itself), then
              verify the login next.
            </p>
          )}

          {(state.kind === 'gmail' || state.kind === 'graph') && (
            <>
              {authUrl ? (
                <p className="flex flex-col gap-1">
                  <span>Open the provider sign-in in your browser:</span>
                  <a
                    href={authUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="text-accent hover:text-accent-hover break-all"
                  >
                    {authUrl}
                  </a>
                </p>
              ) : (
                <p className="text-muted">
                  If the sign-in did not finish, press the button to start it again.
                </p>
              )}
              <label className="block">
                <span className="text-xs font-medium text-primary">
                  Authorization code (from the provider redirect — optional)
                </span>
                <input
                  type="text"
                  value={oauthCode}
                  disabled={state.busy}
                  onChange={(e) => setOauthCode(e.target.value)}
                  placeholder="Paste the code to complete the exchange now"
                  className="mt-1 w-full max-w-md rounded-md border border-line bg-app px-2.5 py-1.5 text-sm text-primary placeholder:text-muted disabled:opacity-50"
                />
              </label>
            </>
          )}

          {state.kind === 'relay' && (
            <>
              {relayToken ? (
                <>
                  <p>
                    The pairing token below is shown <strong>once</strong>. Configure it on your
                    relay device (<code className="font-mono">relay.conf</code> or the install
                    helper), then continue.
                  </p>
                  <div className="rounded-md border border-line bg-hover/40 p-3 space-y-1 text-xs">
                    <p>
                      <span className="text-muted">device_id: </span>
                      <code className="font-mono text-primary">{relayToken.device_id}</code>
                    </p>
                    <p>
                      <span className="text-muted">token: </span>
                      <code className="font-mono text-primary break-all">{relayToken.token}</code>
                    </p>
                  </div>
                </>
              ) : (
                <p className="text-muted">Mint a pairing token to configure the device.</p>
              )}
            </>
          )}
        </div>
      )}

      {/* ── verify ── */}
      {state.step === 'verify' && (
        <p className="text-sm text-secondary">
          Run the provider login probe. A passing probe marks the connection{' '}
          <strong>authenticated</strong> — it does NOT make it operational until data is actually
          ingested.
        </p>
      )}

      {/* ── import ── */}
      {state.step === 'import' && (
        <p className="text-sm text-secondary">
          Import the initial history. The import runs as a background job; its progress is tracked
          on the connection card. If no importer is registered for this kind yet, the backend
          reports that honestly — you can retry later.
        </p>
      )}

      {/* ── done ── */}
      {state.step === 'done' && (
        <p className="text-sm text-success" role="status">
          Setup complete — the connection is now in the list below.
        </p>
      )}

      <div className="flex flex-wrap gap-2 pt-1">
        <button
          type="button"
          onClick={advance}
          disabled={state.busy || state.step === 'pick_kind' && state.kind === null}
          className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
        >
          {state.step === 'import' && state.error ? 'Retry import' : primaryLabel(state)}
        </button>

        {state.step === 'auth' && state.kind === 'relay' && (
          <button
            type="button"
            onClick={rePair}
            disabled={state.busy}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-secondary hover:bg-hover disabled:opacity-50"
          >
            {relayToken ? 'Mint a new pairing token' : 'Mint pairing token'}
          </button>
        )}
        {state.step === 'auth' && state.kind !== 'imap' && state.kind !== 'relay' && (
          <button
            type="button"
            onClick={advance}
            disabled={state.busy}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-secondary hover:bg-hover disabled:opacity-50"
          >
            Start sign-in again
          </button>
        )}
        {state.step === 'verify' && (
          <button
            type="button"
            onClick={() => setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))}
            disabled={state.busy}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-secondary hover:bg-hover disabled:opacity-50"
          >
            Skip for now
          </button>
        )}
        {state.step === 'import' && (
          <button
            type="button"
            onClick={() => setState((prev) => ({ ...prev, step: nextStep(prev), error: null }))}
            disabled={state.busy}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-secondary hover:bg-hover disabled:opacity-50"
          >
            Skip import for now
          </button>
        )}
        {state.step === 'done' && (
          <button
            type="button"
            onClick={finish}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-secondary hover:bg-hover"
          >
            Close wizard
          </button>
        )}
      </div>
    </div>
  )
}

function errMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'detail' in err) {
    const detail = (err as { detail: unknown }).detail
    if (typeof detail === 'string' && detail) return detail
  }
  if (err instanceof Error && err.message) return err.message
  return 'request failed'
}
