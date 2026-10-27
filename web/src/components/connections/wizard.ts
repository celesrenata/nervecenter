/**
 * Connection Center — Add-Connection wizard state machine.
 *
 * Frozen step flow (spec §3 capability 2 "setup wizard steps"):
 *   pick kind → credentials → auth/pairing → verify → import → connected
 *
 * This module is pure (no React, no fetch) so the flow transitions are
 * unit-testable: `canAdvance` / `nextStep` encode which steps are reachable
 * for which provider presets (gmail = OAuth, graph = OAuth, imap =
 * credentials + app password, relay = pairing, which skips verify).
 */

export type WizardKind = 'gmail' | 'imap' | 'graph' | 'relay'

/** Frozen provider presets (kind → display + provider name). */
export const PROVIDER_PRESETS: Array<{
  id: WizardKind
  label: string
  hint: string
  authMode: 'oauth' | 'credentials' | 'pairing'
}> = [
  { id: 'gmail', label: 'Gmail', hint: 'Google account sign-in (OAuth)', authMode: 'oauth' },
  { id: 'imap', label: 'IMAP mailbox', hint: 'Any IMAP host + app password', authMode: 'credentials' },
  { id: 'graph', label: 'Outlook / Microsoft 365', hint: 'Microsoft account sign-in (OAuth)', authMode: 'oauth' },
  { id: 'relay', label: 'Apple Messages relay', hint: 'Pair the macOS Personal Relay device', authMode: 'pairing' },
]

export type WizardStep =
  | 'pick_kind'
  | 'credentials'
  | 'auth'
  | 'verify'
  | 'import'
  | 'done'

export const WIZARD_STEP_ORDER: WizardStep[] = ['pick_kind', 'credentials', 'auth', 'verify', 'import', 'done']

/** The connection kind the backend accepts (workstream B: CONNECTION_KINDS). */
export function backendKindFor(wizardKind: WizardKind): string {
  switch (wizardKind) {
    case 'gmail':
      return 'gmail'
    case 'imap':
      return 'imap'
    case 'graph':
      return 'graph_mail'
    case 'relay':
      return 'relay_device'
  }
}

export interface WizardState {
  step: WizardStep
  kind: WizardKind | null
  /** Non-secret config fields gathered on the credentials step. */
  config: Record<string, string>
  busy: boolean
  error: string | null
}

export function initialWizardState(): WizardState {
  return { step: 'pick_kind', kind: null, config: {}, busy: false, error: null }
}

/** Per-preset credential fields (non-secret only — secrets travel via the
 *  provider flow: OAuth sign-in, app-password pairing, or relay pairing). */
export function fieldsForStep(state: WizardState): Array<{ key: string; label: string; placeholder: string }> {
  if (!state.kind) return []
  switch (state.kind) {
    case 'gmail':
      return [
        { key: 'account', label: 'Google account', placeholder: 'you@gmail.com' },
        { key: 'oauth_redirect_uri', label: 'OAuth redirect URI', placeholder: 'https://your-host/auth/callback' },
      ]
    case 'imap':
      return [
        { key: 'account', label: 'Mailbox address', placeholder: 'you@mail.example' },
        { key: 'host', label: 'IMAP host', placeholder: 'imap.mail.example' },
        { key: 'port', label: 'Port (993 = IMAPS)', placeholder: '993' },
      ]
    case 'graph':
      return [
        { key: 'account', label: 'Microsoft account', placeholder: 'you@outlook.com' },
        { key: 'tenant_id', label: 'Tenant ID (optional)', placeholder: '' },
      ]
    case 'relay':
      return [{ key: 'device_name', label: 'Device name', placeholder: 'Living-room Mac' }]
  }
}

/**
 * Which step follows the current one for the selected preset. The relay
 * preset skips `verify`: a `relay_device` connection has no login probe
 * (the backend answers `probe_unsupported` for it) — pairing via
 * `POST /api/relay/pair` + `register` IS the verification, so its flow is
 * pick → credentials → auth(pairing) → import → done.
 */
export function nextStep(state: WizardState): WizardStep {
  if (!state.kind) return 'pick_kind'
  const order =
    state.kind === 'relay'
      ? (['pick_kind', 'credentials', 'auth', 'import', 'done'] as WizardStep[])
      : WIZARD_STEP_ORDER
  const i = order.indexOf(state.step)
  return order[Math.min(i + 1, order.length - 1)]
}

/** Guard: can the user advance off `state.step`? */
export function canAdvance(state: WizardState): boolean {
  if (state.busy) return false
  switch (state.step) {
    case 'pick_kind':
      return state.kind !== null
    case 'credentials': {
      if (!state.kind) return false
      const required: Record<WizardKind, string[]> = {
        gmail: ['account'],
        imap: ['account', 'host'],
        graph: ['account'],
        relay: [],
      }
      return required[state.kind].every((key) => (state.config[key] ?? '').trim() !== '')
    }
    case 'auth':
      // The auth step completes when the provider flow succeeds; the caller
      // advances it only after a real response (startAuth / pairing).
      return true
    case 'verify':
      // Advances only after a successful verify response (ok:true).
      return true
    case 'import':
      return true
    case 'done':
      return false
  }
}

/**
 * The action the UI must surface at each step — keeps the step → API call
 * mapping explicit and testable:
 *   credentials → createConnection (then auth)
 *   auth        → startAuth (OAuth url / pairing) or relay pairing
 *   verify      → verifyConnection
 *   import      → startImport
 *   done        → list (the row is in the card list)
 */
export function stepAction(state: WizardState): {
  kind: 'pick' | 'create' | 'auth' | 'verify' | 'import' | 'done'
  label: string
} {
  switch (state.step) {
    case 'pick_kind':
      return { kind: 'pick', label: 'Continue' }
    case 'credentials':
      return { kind: 'create', label: 'Save & start sign-in' }
    case 'auth':
      return state.kind === 'relay'
        ? { kind: 'auth', label: 'I paired the device' }
        : { kind: 'auth', label: 'I finished sign-in' }
    case 'verify':
      return { kind: 'verify', label: 'Verify connection' }
    case 'import':
      return { kind: 'import', label: 'Start initial import' }
    case 'done':
      return { kind: 'done', label: 'Done' }
  }
}
