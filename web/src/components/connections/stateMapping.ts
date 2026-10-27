/**
 * Connection Center — frozen five-state mapping (spec `personal-ops-recovery` §3).
 *
 * The backend (workstream B) derives `state` from the two orthogonal axes
 * `auth_status` × `operational_status` and sends it on every connection row.
 * The UI renders **both axes as two separate badges** plus the derived state
 * label. The hard rule: a connection that passed a login probe but has
 * never ingested data is `never_synced` on the operational axis and MUST
 * NOT render as operational/connected — its state is `connected` only in
 * the frozen derivation sense of "auth is fine, no error recorded"; the
 * operational badge always shows "Never synced".
 *
 * `deriveConnectionState` mirrors `backend.connectors.registry.derive_state`
 * and is used as a local cross-check: if the payload lacks a server `state`
 * (should never happen on the frozen contract) we fall back to deriving
 * it, so a client can never render a row without one of the five states.
 */

export const CONNECTION_STATES = [
  'connected',
  'syncing',
  'degraded',
  'auth_required',
  'not_configured',
] as const
export type ConnectionState = (typeof CONNECTION_STATES)[number]

export const AUTH_STATUSES = [
  'unconfigured',
  'pairing_pending',
  'authenticated',
  'auth_expired',
  'auth_failed',
] as const
export type AuthStatus = (typeof AUTH_STATUSES)[number]

export const OPERATIONAL_STATUSES = [
  'never_synced',
  'syncing',
  'healthy',
  'degraded',
  'error',
] as const
export type OperationalStatus = (typeof OPERATIONAL_STATUSES)[number]

export interface AuthBadge {
  label: string
  tone: 'neutral' | 'success' | 'warning' | 'danger'
}

export interface OperationalBadge {
  label: string
  tone: 'neutral' | 'success' | 'warning' | 'danger'
}

/** state → the two badges the UI renders (auth axis + operational axis). */
export interface StatePresentation {
  state: ConnectionState
  label: string
  stateTone: 'neutral' | 'success' | 'warning' | 'danger'
  auth: AuthBadge
  operational: OperationalBadge
}

/**
 * Mirror of `derive_state` (registry.py:239). Precedence:
 * 1. operational `syncing` → `syncing` (outranks every auth axis)
 * 2. `unconfigured` auth   → `not_configured`
 * 3. `auth_expired`/`auth_failed` → `auth_required`
 * 4. any other non-final auth (`pairing_pending`, …) → `auth_required`
 * 5. authenticated + `healthy` → `connected`
 * 6. authenticated + (`degraded` | `error` | `never_synced` with error) → `degraded`
 * 7. authenticated + `never_synced` with no error → `connected`
 */
export function deriveConnectionState(
  authStatus: string | null | undefined,
  operationalStatus: string | null | undefined,
  hasError: boolean,
): ConnectionState {
  const auth = (authStatus ?? 'unconfigured').trim()
  const op = (operationalStatus ?? 'never_synced').trim()
  if (op === 'syncing') return 'syncing'
  if (auth === 'unconfigured') return 'not_configured'
  if (auth === 'auth_expired' || auth === 'auth_failed') return 'auth_required'
  if (auth !== 'authenticated') return 'auth_required'
  if (op === 'healthy') return 'connected'
  if (op === 'degraded' || op === 'error' || (op === 'never_synced' && hasError)) return 'degraded'
  return 'connected'
}

function hasRecordedError(
  lastError: string | null | undefined,
  lastSuccessAt: string | null | undefined,
): boolean {
  return Boolean(lastError) && !lastSuccessAt
}

/**
 * Map a connection payload to the frozen presentation: the five-state label
 * plus the TWO SEPARATE badges (authentication status and operational
 * status). `last_error_code` / `last_success_at` participate in the
 * derivation exactly as the backend does (`has_error` = error recorded and
 * no successful sync yet).
 */
export function mapConnectionState(input: {
  state?: string | null
  auth_status?: string | null
  operational_status?: string | null
  last_error_code?: string | null
  last_success_at?: string | null
}): StatePresentation {
  const { state, auth_status: auth, operational_status: op, last_error_code, last_success_at } =
    input
  const derived = deriveConnectionState(auth, op, hasRecordedError(last_error_code, last_success_at))
  const effective: ConnectionState =
    state === undefined || state === null || state === '' || !(CONNECTION_STATES as readonly string[]).includes(state)
      ? derived
      : (state as ConnectionState)

  const authBadge: AuthBadge =
    auth === 'authenticated'
      ? { label: 'Authenticated', tone: 'success' }
      : auth === 'pairing_pending'
        ? { label: 'Pairing pending', tone: 'warning' }
        : auth === 'auth_expired'
          ? { label: 'Authentication expired', tone: 'danger' }
          : auth === 'auth_failed'
            ? { label: 'Authentication failed', tone: 'danger' }
            : { label: 'Not configured', tone: 'neutral' }

  const operationalBadge: OperationalBadge =
    op === 'healthy'
      ? { label: 'Healthy', tone: 'success' }
      : op === 'syncing'
        ? { label: 'Syncing', tone: 'warning' }
        : op === 'degraded'
          ? { label: 'Degraded', tone: 'warning' }
          : op === 'error'
            ? { label: 'Error', tone: 'danger' }
            : op === 'never_synced'
              ? { label: 'Never synced', tone: 'neutral' }
              : { label: 'Unknown', tone: 'neutral' }

  const presentation: Record<ConnectionState, StatePresentation> = {
    connected: {
      state: 'connected',
      label: 'Connected',
      stateTone: 'success',
      auth: authBadge,
      operational: operationalBadge,
    },
    syncing: {
      state: 'syncing',
      label: 'Syncing',
      stateTone: 'warning',
      auth: authBadge,
      operational: operationalBadge,
    },
    degraded: {
      state: 'degraded',
      label: 'Degraded',
      stateTone: 'warning',
      auth: authBadge,
      operational: operationalBadge,
    },
    auth_required: {
      state: 'auth_required',
      label: 'Authentication Required',
      stateTone: 'danger',
      auth: authBadge,
      operational: operationalBadge,
    },
    not_configured: {
      state: 'not_configured',
      label: 'Not Configured',
      stateTone: 'neutral',
      auth: authBadge,
      operational: operationalBadge,
    },
  }
  return presentation[effective]
}

/**
 * Relative + absolute rendering of a timestamp for the
 * "last successful sync" line. Returns `null` when the value is absent or
 * unparsable (the caller then renders the "never ingested" copy).
 */
export function formatRelative(iso: string | null | undefined): {
  relative: string
  absolute: string
} | null {
  if (!iso) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  const now = Date.now()
  const diffMs = now - date.getTime()
  const minutes = Math.round(diffMs / 60_000)
  let relative: string
  if (diffMs < 0 || minutes < 1) relative = 'just now'
  else if (minutes < 60) relative = `${minutes} min ago`
  else {
    const hours = Math.round(minutes / 60)
    if (hours < 24) relative = `${hours} hr${hours === 1 ? '' : 's'} ago`
    else {
      const days = Math.round(hours / 24)
      relative = `${days} day${days === 1 ? '' : 's'} ago`
    }
  }
  return { relative, absolute: date.toLocaleString() }
}
