/**
 * API client with env-based mock mode.
 * If VITE_API_BASE is set → real API; otherwise → mock fixtures.
 */

import { mockOverview, mockTasks, mockAgents, mockComputeNodes, mockRoutes, mockModelHistory, mockIncidents, mockDecisions, mockEvents, mockSystems, mockAskResponse, mockGoals, mockModels } from './mockClient'

// Determine mode based on environment variable
const API_BASE = import.meta.env?.VITE_API_BASE

export const useMockMode = API_BASE === undefined || API_BASE === ''

// ─── Briefing (prompt2 §30 — Briefings) ────────────────────────────────────────

export type BriefingKind = 'morning' | 'midday' | 'evening'

export interface BriefingAction {
  attention_id: string
  label: string
  due: string | null
}

export interface BriefingWaitingOn {
  request_id: string
  person: string
  description: string
  days_elapsed: number
}

export interface BriefingEvent {
  event_id: string
  title: string
  start: string | null
}

export interface BriefingOvernight {
  emails: number
  messages: number
  voicemails: number
}

export interface Briefing {
  kind: BriefingKind
  generated_at: string
  actions: BriefingAction[]
  waiting_on: BriefingWaitingOn[]
  today: BriefingEvent[]
  overnight: BriefingOvernight
  empty: boolean
}

export interface ApiResponse<T> {
  data: T
  status: number
}

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Payload for creating an AttentionItem (prompt2 Phase P0). Mirrors the
 * backend `AttentionItem` model minus the server-generated fields
 * (attention_id, priority, created_at, updated_at, why).
 */
export interface AttentionCreatePayload {
  reason: string
  urgency?: number
  importance?: number
  due?: string | null
  entities?: string[]
  sources?: string[]
  suggested_action?: string | null
  status?: string
  why?: string[]
}

/**
 * Mock AttentionItems (prompt2 Phase P0). Mirrors the demo queue the backend
 * seeds via `seed_demo()`, so the Now screen renders the same ranked items in
 * both mock mode (no `VITE_API_BASE`) and real mode.
 */
const mockAttention = [
  {
    attention_id: 'att-mock-pharmacy',
    reason: 'Call the pharmacy before their 5 PM cutoff',
    priority: 1.0,
    urgency: 0.95,
    importance: 0.9,
    due: null,
    entities: ['Pharmacy'],
    sources: ['calendar'],
    suggested_action: 'Call the pharmacy',
    status: 'NOW',
    why: [
      'base 0.2025 from importance 0.90 + 0.2375 from urgency 0.95',
      'deadline proximity: due within a few hours',
      'direct request: a concrete action is expected',
    ],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
  {
    attention_id: 'att-mock-alice',
    reason: "Reply to Alice about tomorrow's plan",
    priority: 1.0,
    urgency: 0.95,
    importance: 0.9,
    due: null,
    entities: ['Alice'],
    sources: ['email'],
    suggested_action: "Reply to Alice",
    status: 'NOW',
    why: [
      'base 0.2025 from importance 0.90 + 0.2375 from urgency 0.95',
      'direct request: a concrete action is expected',
    ],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
  {
    attention_id: 'att-mock-deploy',
    reason: 'Approve the HelloDJ deployment',
    priority: 0.92,
    urgency: 0.9,
    importance: 0.8,
    due: null,
    entities: ['HelloDJ'],
    sources: ['deploy-pipeline'],
    suggested_action: 'Approve the deployment',
    status: 'NOW',
    why: [
      'base 0.18 from importance 0.80 + 0.225 from urgency 0.90',
      'direct request: a concrete action is expected',
    ],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
  {
    attention_id: 'att-mock-flight',
    reason: 'Flight rebooking confirmation pending',
    priority: 0.59,
    urgency: 0.55,
    importance: 0.5,
    due: null,
    entities: ['Flight'],
    sources: ['email'],
    suggested_action: 'Confirm the rebooking',
    status: 'NOW',
    why: ['base 0.125 from importance 0.50 + 0.1375 from urgency 0.55'],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
]

// Demonstration data (mock transport only — never served by the real API):
// one iMessage thread with an unread inbound message.
const mockMessages = [
  {
    conversation_id: 'conv-+14155550134|imessage',
    peer_identifier: '+14155550134',
    peer_name: 'Alice Nguyen',
    channel: 'imessage',
    message_count: 2,
    unread: 1,
    last_body: 'Are you still free to review the deck Friday?',
    last_direction: 'INBOUND',
    last_timestamp: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
    attention_id: null,
    created_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    updated_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
  },
]

// Demonstration data (mock transport only — never served by the real API):
// one email thread with an unread inbound email, mirroring the thread the
// backend seeds via `NERVE_SEED_EMAILS=1` / `seed_demo()`.
const mockEmailThreads = [
  {
    thread_id: 'thr-alice.n@example.com|q4 planning — heads up',
    from_address: 'alice.n@example.com',
    to_address: 'me@example.com',
    sender_name: 'Alice Nguyen',
    subject: 'Q4 planning — heads up',
    message_count: 2,
    unread: 2,
    last_body: 'No rush — Monday works too if Friday is gone.',
    last_direction: 'RECEIVED',
    last_timestamp: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
    attention_id: null,
    created_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    updated_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
  },
]

// Demonstration data (mock transport only — never served by the real API):
// two voicemail threads mirroring the thread the backend seeds via
// `NERVE_SEED_VOICEMAILS=1` / `seed_demo()` — one carrying a callback-request
// ask (§15/§77) and one ordinary message.
const mockVoicemails = [
  {
    thread_id: 'vthr-14155550134',
    caller_phone: '+1 415-555-0134',
    caller_name: 'Dr. Smith',
    voicemail_count: 1,
    unread: 1,
    last_transcript: 'Please call us regarding your appointment.',
    last_duration_seconds: 12,
    last_received_at: new Date(Date.now() - 15 * 60 * 1000).toISOString(),
    needs_callback: true,
    callback_reason: 'please call us',
    attention_id: null,
    created_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    updated_at: new Date(Date.now() - 15 * 60 * 1000).toISOString(),
  },
  {
    thread_id: 'vthr-12025550100',
    caller_phone: '+1 202-555-0100',
    caller_name: 'Alice Nguyen',
    voicemail_count: 1,
    unread: 1,
    last_transcript: 'Hi — just confirming our meeting is still on for Thursday.',
    last_duration_seconds: 18,
    last_received_at: new Date(Date.now() - 40 * 60 * 1000).toISOString(),
    needs_callback: false,
    callback_reason: null,
    attention_id: null,
    created_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    updated_at: new Date(Date.now() - 40 * 60 * 1000).toISOString(),
  },
]

// Demonstration data (mock transport only — never served by the real API):
// three waiting-on requests mirroring the requests the backend seeds via
// `NERVE_SEED_WAITING_ON=1` / `seed_demo()` — two OPEN (one that has been
// waiting 3 days, one 1 day) and one already RESOLVED, so the Waiting-On
// screen renders both card states in mock mode (prompt2 §19/§31).
interface MockWaitingOnRecord {
  request_id: string
  person_display_name: string
  person_id: string | null
  description: string
  requested_at: string
  source: string
  source_thread_id: string | null
  status: string
  attention_id: string | null
  follow_up_count: number
  last_follow_up_at: string | null
  resolved_at: string | null
  note: string | null
  created_at: string
  updated_at: string
  escalation_policy: Record<string, unknown> | null
}
const mockWaitingOn: MockWaitingOnRecord[] = [
  {
    request_id: 'wo-seed-sam-creds',
    person_display_name: 'Sam',
    person_id: null,
    description: 'deployment credentials',
    requested_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
    source: 'messages',
    source_thread_id: 'thread-sam',
    status: 'open',
    attention_id: null,
    follow_up_count: 0,
    last_follow_up_at: null,
    resolved_at: null,
    note: null,
    created_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
    updated_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
    // Urgent escalation (mirrors backend seed_demo §19): 3 days unanswered
    // → follow up within 24h, or escalate to a VIP if unresolved.
    escalation_policy: { priority: 'urgent' },
  },
  {
    request_id: 'wo-seed-insurance-claim',
    person_display_name: 'Insurance',
    person_id: null,
    description: 'claim response',
    requested_at: new Date(Date.now() - 1 * 24 * 60 * 60 * 1000).toISOString(),
    source: 'email',
    source_thread_id: null,
    status: 'open',
    attention_id: null,
    follow_up_count: 0,
    last_follow_up_at: null,
    resolved_at: null,
    note: null,
    created_at: new Date(Date.now() - 1 * 24 * 60 * 60 * 1000).toISOString(),
    updated_at: new Date(Date.now() - 1 * 24 * 60 * 60 * 1000).toISOString(),
    escalation_policy: null,
  },
  {
    request_id: 'wo-seed-alice-review',
    person_display_name: 'Alice Nguyen',
    person_id: null,
    description: 'deck review',
    requested_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
    source: 'email',
    source_thread_id: null,
    status: 'resolved',
    attention_id: null,
    follow_up_count: 0,
    last_follow_up_at: null,
    resolved_at: new Date(Date.now() - 0.5 * 24 * 60 * 60 * 1000).toISOString(),
    note: 'Alice reviewed the deck',
    created_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
    updated_at: new Date(Date.now() - 0.5 * 24 * 60 * 60 * 1000).toISOString(),
    escalation_policy: null,
  },
]

// Demonstration data (mock transport only — never served by the real API):
// three resolved people mirroring the people the backend seeds via
// `NERVE_SEED_CONTACTS=1` / `seed_demo()` — one of them a VIP (§12).
const mockContacts = [
  {
    person_id: 'alice-nguyen',
    display_name: 'Alice Nguyen',
    emails: ['alice.n@example.com'],
    phones: ['+1 415-555-0134'],
    notes: 'Design partner — prefers email.',
    is_vip: false,
    source: 'personal-relay',
    created_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    last_updated_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
  },
  {
    person_id: 'bob-cho',
    display_name: 'Bob Cho',
    emails: ['bob.cho@example.com'],
    phones: ['+1 202-555-0199'],
    notes: null,
    is_vip: false,
    source: 'personal-relay',
    created_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    last_updated_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
  },
  {
    person_id: 'dr-smith',
    display_name: 'Dr. Smith',
    emails: ['frontdesk@smithclinic.example.com'],
    phones: ['+1 415-555-0100'],
    notes: 'Call before 5 PM — clinic closes early.',
    is_vip: true,
    source: 'manual',
    created_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    last_updated_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
  },
]

// One upcoming event + one overlapping event (mock transport only): lets
// /calendar demonstrate availability + conflict reasoning (§42/§43) with no
// real API. The 18:00–19:00 event overlaps the 18:30 request below.
const _today = new Date()
const _calDay = `${_today.toISOString().slice(0, 10)}`
const mockCalendar = [
  {
    event_id: 'ev-mock-design-review',
    title: 'Design review',
    description: null,
    start: `${_calDay}T16:00:00.000Z`,
    end: `${_calDay}T17:00:00.000Z`,
    all_day: false,
    location: 'Room 4',
    participants: ['alice@example.com'],
    reminder_at: null,
    source: 'calendar-sync',
    sync_id: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
  {
    event_id: 'ev-mock-dinner',
    title: 'Dinner with family',
    description: null,
    start: `${_calDay}T18:00:00.000Z`,
    end: `${_calDay}T19:00:00.000Z`,
    all_day: false,
    location: 'Home',
    participants: [],
    reminder_at: null,
    source: 'calendar-sync',
    sync_id: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
]

// ─── Error type (gates OV-1 / TA-1 / DC-1) ───────────────────────────────────
//
// Every real-API call that receives a non-2xx response rejects with this
// error. `status` is the HTTP status; `detail` is the parsed FastAPI
// `detail` body (or the status text when the body carries none). The old
// behavior — mapping error responses to empty arrays — silently rendered
// 500/501 endpoints as "no data"; views now see a real error they can
// surface.
export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string, message?: string) {
    super(message ?? `API request failed: ${status} ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

// ─── Auth transport (S102: SSO + authorized fetches) ──────────────────────────

/**
 * Session-scoped token storage. Tokens live in sessionStorage (NOT localStorage)
 * so a closed tab ends the session — the security model the SSO spec calls for.
 */
const ACCESS_TOKEN_KEY = 'nc.accessToken'
const REFRESH_TOKEN_KEY = 'nc.refreshToken'
/** Code-verifier + state for the in-flight PKCE authorization-code exchange. */
const PKCE_VERIFIER_KEY = 'nc.pkce.codeVerifier'
const PKCE_STATE_KEY = 'nc.pkce.state'

function safeGetSession(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key)
  } catch {
    return null
  }
}

function safeSetSession(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value)
  } catch {
    /* sessionStorage unavailable (private mode) — ignore, session is ephemeral anyway */
  }
}

function safeRemoveSession(key: string): void {
  try {
    window.sessionStorage.removeItem(key)
  } catch {
    /* ignore */
  }
}

export function getAccessToken(): string | null {
  return safeGetSession(ACCESS_TOKEN_KEY)
}

/** Store a fresh access token (from break-glass login or the SSO token exchange). */
export function setAccessToken(token: string | null, refreshToken?: string | null): void {
  if (token) safeSetSession(ACCESS_TOKEN_KEY, token)
  else safeRemoveSession(ACCESS_TOKEN_KEY)
  if (refreshToken) safeSetSession(REFRESH_TOKEN_KEY, refreshToken)
  else safeRemoveSession(REFRESH_TOKEN_KEY)
}

/** Clear the whole session (logout or an un-recoverable 401). */
export function clearSessionTokens(): void {
  safeRemoveSession(ACCESS_TOKEN_KEY)
  safeRemoveSession(REFRESH_TOKEN_KEY)
  safeRemoveSession(PKCE_VERIFIER_KEY)
  safeRemoveSession(PKCE_STATE_KEY)
}

/** The in-flight refresh token, if any (kept for the session; unused while
 * the backend exposes no refresh route — see apiFetch 401 handling). */
export function getRefreshToken(): string | null {
  return safeGetSession(REFRESH_TOKEN_KEY)
}

/**
 * The 401 handler is registered by AuthProvider so the API layer can force a
 * redirect to /login without importing React (avoids a module cycle).
 */
export type UnauthorizedHandler = () => void
let unauthorizedHandler: UnauthorizedHandler | null = null
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

/**
 * The single fetch wrapper every real-API request goes through. Adds
 * `Authorization: Bearer <token>` (S102: the token comes from
 * sessionStorage `nc.accessToken`) when a session token is present.
 *
 * S102 401 handling: the backend exposes **no** token-refresh route
 * (backend/api/auth.py: login, me, logout, pairing only), so a 401 on an
 * authorized call is terminal — the sessionStorage session is cleared and
 * the caller surfaces ApiError(status=401) (realApi raises it via
 * responseOrThrow / listFromResponse). The break-glass login itself is
 * excluded: its 401 is a credential error the form should surface, not a
 * session invalidation.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const base = (API_BASE ?? '').replace(/\/+$/, '')
  // Route normalization (personal-ops-recovery, Area 4 e2e): the canonical
  // deployment serves the API under `/api` (VITE_API_BASE=/api — the k8s web
  // nginx upstream AND the e2e vite proxy). The older house clients pass
  // unprefixed routes (`/attention`, `/calendar`) while the newer workstream
  // clients pass the full `/api/...` path; without this the latter would
  // double-prefix to `/api/api/...` and 404. Collapse the overlap so both
  // styles resolve to the same canonical URL. Absolute URLs pass through.
  let url: string
  if (path.startsWith('http')) {
    url = path
  } else if (base && (path === base || path.startsWith(`${base}/`))) {
    url = path // already carries the API base
  } else if (base && path.startsWith('/')) {
    url = `${base}${path}`
  } else {
    url = path
  }
  const headers = new Headers(init.headers)
  const token = getAccessToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  const res = await fetch(url, { ...init, headers })
  if (res.status === 401) {
    if (!path.startsWith('/auth/login')) {
      clearSessionTokens()
      if (unauthorizedHandler) unauthorizedHandler()
    }
  }
  return res
}

// ─── Real API implementation ──────────────────────────────────────────────────

/**
 * Parse a FastAPI error body ({detail: ...}) and build the ApiError.
 * Exported so callers outside the client (AuthContext login, the
 * /auth/callback exchange, tests) can surface non-2xx responses with the
 * same status+detail shape the real-API layer uses.
 */
export async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  let detail = res.statusText || `HTTP ${res.status}`
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail.length > 0) {
      detail = body.detail
    } else if (body.detail !== undefined) {
      detail = JSON.stringify(body.detail)
    }
  } catch {
    /* non-JSON body — keep the status text */
  }
  return new ApiError(res.status, detail)
}

/** Non-2xx → reject with ApiError; 2xx → return the parsed JSON body. */
async function responseOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

/**
 * Extract the item list from a real API list response.
 *
 * The backend list endpoints (§46) wrap collections in a
 * `{ data: [...], meta: {...} }` envelope, while the views (and the mock
 * fixtures) consume plain arrays. Non-list bodies — a single-item object
 * (e.g. `/models/history` when the backend resolves it as one resource) or an
 * unparsable body — degrade to an empty list so that a view renders its
 * empty state instead of crashing on `.map()`.
 *
 * Error responses (non-2xx) NO LONGER degrade to `[]`: they reject with
 * `ApiError` (gates OV-1/TA-1/DC-1). Previously a `/api/tasks` 500 or an
 * `/api/overview` 501 rendered as silent zeros; now the views' `catch`
 * blocks see the real failure.
 */
// Returns `any[]` (not `unknown[]`) so the views keep assigning
// `response.data` to their typed state exactly as the mock fixtures do.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const listFromResponse = async (res: Response): Promise<any[]> => {
  if (!res.ok) throw await apiErrorFromResponse(res)
  let body: any
  try {
    body = await res.json()
  } catch {
    return []
  }
  if (Array.isArray(body)) return body
  if (
    body !== null &&
    typeof body === 'object' &&
    Array.isArray((body as { data?: unknown }).data)
  ) {
    return (body as { data: unknown[] }).data
  }
  return []
}

// ─── SSO PKCE helpers (S102: /auth/callback) ──────────────────────────────────

/** AC-S01 shape of GET /api/auth/sso/config. */
export interface SsoConfig {
  enabled: boolean
  issuer: string
  client_id: string
  authorization_endpoint: string
  token_endpoint: string
  userinfo_endpoint?: string | null
  jwks_uri?: string | null
  end_session_endpoint?: string | null
}

/** Fetch the SSO/OIDC discovery config from the backend (or null on error). */
export async function getSsoConfig(): Promise<SsoConfig | null> {
  const res = await apiFetch('/auth/sso/config')
  if (!res.ok) return null
  return (await res.json()) as SsoConfig
}

function base64Url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

/** Generate a cryptographically random code verifier (RFC 7636 §4.1). */
export function generatePkceVerifier(): string {
  return base64Url(crypto.getRandomValues(new Uint8Array(32)))
}

/** Store the in-flight PKCE code verifier + state in sessionStorage. */
export function storePkceChallenge(verifier: string, state: string): void {
  safeSetSession(PKCE_VERIFIER_KEY, verifier)
  safeSetSession(PKCE_STATE_KEY, state)
}

/** The state value stored at authorization start (null if none). */
export function getPkceState(): string | null {
  return safeGetSession(PKCE_STATE_KEY)
}

function consumePkceVerifier(): string | null {
  const verifier = safeGetSession(PKCE_VERIFIER_KEY)
  if (verifier) safeRemoveSession(PKCE_VERIFIER_KEY)
  return verifier
}

/**
 * Build the SSO provider's authorization URL (PKCE S256 authorization-code
 * flow, AC-S06). Stores the code verifier + state in sessionStorage so
 * `exchangeAuthCode` can validate the round-trip on the callback.
 * Returns null when SSO is not enabled (provider not yet provisioned).
 */
export async function getAuthorizationUrl(): Promise<string | null> {
  const cfg = await getSsoConfig()
  if (!cfg?.enabled || !cfg.authorization_endpoint) return null
  const verifier = generatePkceVerifier()
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(verifier),
  )
  const challenge = base64Url(new Uint8Array(digest))
  const state = base64Url(crypto.getRandomValues(new Uint8Array(16)))
  storePkceChallenge(verifier, state)
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: cfg.client_id,
    redirect_uri: window.location.origin + '/auth/callback',
    scope: 'openid profile',
    code_challenge: challenge,
    code_challenge_method: 'S256',
    state,
  })
  return `${cfg.authorization_endpoint}?${params.toString()}`
}

/**
 * Exchange the PKCE authorization code for tokens at the SSO provider's
 * token endpoint (the "backend token route" per AC-S01) and store the
 * session tokens (nc.accessToken / nc.refreshToken).
 *
 * Steps: validate the round-tripped `state` against the stored
 * `nc.pkce.state`, consume the code verifier, POST the code exchange,
 * store the returned access + refresh tokens in sessionStorage.
 * Rejects with ApiError on a state mismatch, a missing verifier, a
 * failed token exchange, or a response without an access token.
 */
export async function exchangeAuthCode(
  code: string,
  state: string | null,
): Promise<{ accessToken: string; refreshToken: string | null }> {
  const expectedState = getPkceState()
  if (!expectedState || expectedState !== state) {
    clearSessionTokens()
    throw new ApiError(400, 'PKCE state mismatch (possible CSRF)')
  }
  const verifier = consumePkceVerifier()
  if (!verifier) {
    clearSessionTokens()
    throw new ApiError(400, 'missing PKCE code verifier')
  }
  const cfg = await getSsoConfig()
  if (!cfg?.token_endpoint) {
    clearSessionTokens()
    throw new ApiError(500, 'no SSO token_endpoint configured')
  }
  const params = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    code_verifier: verifier,
    redirect_uri: window.location.origin + '/auth/callback',
    client_id: cfg.client_id,
  })
  const res = await fetch(cfg.token_endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: params.toString(),
  })
  if (!res.ok) throw await apiErrorFromResponse(res)
  const body = (await res.json()) as {
    access_token?: string
    refresh_token?: string | null
  }
  if (!body.access_token) {
    clearSessionTokens()
    throw new ApiError(res.status, 'SSO token exchange returned no access_token')
  }
  setAccessToken(body.access_token, body.refresh_token ?? null)
  return { accessToken: body.access_token, refreshToken: body.refresh_token ?? null }
}

const realApi = {
  // Health & Overview
  getOverview: async () => {
    const res = await apiFetch('/overview')
    return { data: await responseOrThrow(res), status: res.status }
  },
  getHealth: async () => {
    const res = await apiFetch('/health')
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Tasks (Task DAG with 8 states)
  getTasks: async () => {
    const res = await apiFetch('/tasks')
    return { data: await listFromResponse(res), status: res.status }
  },
  getTask: async (taskId: string) => {
    const res = await apiFetch(`/tasks/${taskId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Agents
  getAgents: async () => {
    const res = await apiFetch('/agents')
    return { data: await listFromResponse(res), status: res.status }
  },
  getAgent: async (agentId: string) => {
    const res = await apiFetch(`/agents/${agentId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Compute nodes (for Inference view)
  getComputeNodes: async () => {
    const res = await apiFetch('/compute')
    return { data: await listFromResponse(res), status: res.status }
  },
  getComputeNode: async (nodeId: string) => {
    const res = await apiFetch(`/compute/${nodeId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },

  // OmniRoute routes (for Inference view)
  getRoutes: async () => {
    const res = await apiFetch('/routes')
    return { data: await listFromResponse(res), status: res.status }
  },
  getRoute: async (routeId: string) => {
    const res = await apiFetch(`/routes/${routeId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Historical model performance (for Inference view)
  getModelHistory: async () => {
    const res = await apiFetch('/models/history')
    return { data: await listFromResponse(res), status: res.status }
  },

  // Incidents
  getIncidents: async () => {
    const res = await apiFetch('/incidents')
    return { data: await listFromResponse(res), status: res.status }
  },
  getIncident: async (incidentId: string) => {
    const res = await apiFetch(`/incidents/${incidentId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Decisions
  getDecisions: async () => {
    const res = await apiFetch('/decisions')
    return { data: await listFromResponse(res), status: res.status }
  },
  getDecision: async (decisionId: string) => {
    const res = await apiFetch(`/decisions/${decisionId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Events (Timeline)
  getEvents: async () => {
    // Trailing slash is required: the backend route is registered on
    // `/api/events/` and the slashless form answers with a 307 whose absolute
    // Location drops the port/scheme, which fails the browser follow
    // (SY fix; mirrors getSystems). Without it the live Timeline view shows a
    // spurious "network error" alert alongside the empty state.
    const res = await apiFetch('/events/')
    return { data: await listFromResponse(res), status: res.status }
  },
  getEvent: async (eventId: string) => {
    const res = await apiFetch(`/events/${eventId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Systems health
  getSystems: async () => {
    // Trailing slash is required: the backend route is registered on
    // `/api/systems/` and the slashless form answers with a 307 whose absolute
    // Location drops the port/scheme, which fails the browser follow (SY fix).
    const res = await apiFetch('/systems/')
    return { data: await listFromResponse(res), status: res.status }
  },
  getMetrics: async () => {
    const res = await apiFetch('/metrics')
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Ask endpoint — §47 `nerve ask` → POST /api/context/ask.
  // Returns the full answer (200) or the accepted-but-not-executed ack
  // (202, `{accepted, executed, detail, request_id, ...}`); AskView renders
  // whichever shape arrives.
  postAsk: async (message: string) => {
    const res = await apiFetch('/context/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // The T003-04 AskRequest is `extra: "forbid"` — only `question` (plus an
      // optional `context`) is accepted. The legacy `message` alias is no
      // longer part of the contract and would trip the 422 rejection.
      body: JSON.stringify({ question: message }),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Goals
  getGoals: async () => {
    const res = await apiFetch('/goals')
    return { data: await listFromResponse(res), status: res.status }
  },
  getGoal: async (goalId: string) => {
    const res = await apiFetch(`/goals/${goalId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },
  deleteGoal: async (goalId: string) => {
    const res = await apiFetch(`/goals/${goalId}`, { method: 'DELETE' })
    if (!res.ok && res.status !== 204) throw await apiErrorFromResponse(res)
    return { data: null, status: res.status }
  },

  // Attention (prompt2 Phase P0 — Personal Operations Plane)
  getAttention: async (status?: string) => {
    const qs = status ? `?status=${encodeURIComponent(status)}` : ''
    const res = await apiFetch(`/attention${qs}`)
    return { data: await listFromResponse(res), status: res.status }
  },
  getAttentionNow: async () => {
    const res = await apiFetch('/attention/now')
    return { data: await listFromResponse(res), status: res.status }
  },
  createAttention: async (payload: AttentionCreatePayload) => {
    const res = await apiFetch('/attention', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  attentionAction: async (
    attentionId: string,
    action: 'resolve' | 'suppress' | 'acknowledge' | 'snooze',
  ) => {
    const body = action === 'snooze' ? JSON.stringify({}) : undefined
    const res = await apiFetch(`/attention/${attentionId}/${action}`, {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body,
    })
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Messages (prompt2 Phase P1 — Personal Operations Plane)
  getMessages: async (channel?: string) => {
    const qs = channel ? `?channel=${encodeURIComponent(channel)}` : ''
    const res = await apiFetch(`/messages${qs}`)
    return { data: await listFromResponse(res), status: res.status }
  },
  getMessageThread: async (conversationId: string) => {
    const res = await apiFetch(`/messages/${conversationId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },
  ingestMessage: async (payload: Record<string, unknown>) => {
    const res = await apiFetch('/messages/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  acknowledgeMessage: async (conversationId: string) => {
    const res = await apiFetch(`/messages/${conversationId}/acknowledge`, { method: 'POST' })
    return { data: await responseOrThrow(res), status: res.status }
  },
  draftReply: async (conversationId: string) => {
    const res = await apiFetch(`/messages/${conversationId}/draft`, { method: 'POST' })
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Email (prompt2 Phase P0 — Personal Operations Plane)
  getEmailThreads: async () => {
    const res = await apiFetch('/email')
    return { data: await listFromResponse(res), status: res.status }
  },
  getEmailThread: async (threadId: string) => {
    const res = await apiFetch(`/email/${threadId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },
  ingestEmail: async (payload: Record<string, unknown>) => {
    const res = await apiFetch('/email/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  acknowledgeEmail: async (threadId: string) => {
    const res = await apiFetch(`/email/${threadId}/acknowledge`, { method: 'POST' })
    return { data: await responseOrThrow(res), status: res.status }
  },
  draftEmailReply: async (threadId: string) => {
    const res = await apiFetch(`/email/${threadId}/draft`, { method: 'POST' })
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Voicemail (prompt2 Phase P2 — Personal Operations Plane)
  getVoicemailThreads: async () => {
    const res = await apiFetch('/voicemail')
    return { data: await listFromResponse(res), status: res.status }
  },
  getVoicemailThread: async (threadId: string) => {
    const res = await apiFetch(`/voicemail/${threadId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },
  ingestVoicemail: async (payload: Record<string, unknown>) => {
    const res = await apiFetch('/voicemail/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  acknowledgeVoicemail: async (threadId: string) => {
    const res = await apiFetch(`/voicemail/${threadId}/acknowledge`, { method: 'POST' })
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Calendar (prompt2 Phase P2 — Calendar Integration + Conflict Reasoning)
  getCalendar: async (params?: { start?: string; end?: string; limit?: number }) => {
    const qs = new URLSearchParams()
    if (params?.start) qs.set('start', params.start)
    if (params?.end) qs.set('end', params.end)
    if (params?.limit) qs.set('limit', String(params.limit))
    const q = qs.toString()
    const res = await apiFetch(`/calendar${q ? `?${q}` : ''}`)
    return { data: await listFromResponse(res), status: res.status }
  },
  ingestCalendarEvent: async (payload: Record<string, unknown>) => {
    const res = await apiFetch('/calendar/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  calendarAvailability: async (start: string, end: string) => {
    const res = await apiFetch('/calendar/availability', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start, end }),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  calendarFreeTime: async (start: string, durationMinutes: number, horizonMinutes?: number) => {
    const res = await apiFetch('/calendar/free-time', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start, duration_minutes: durationMinutes, horizon_minutes: horizonMinutes ?? null }),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  calendarSuggest: async (start: string, durationMinutes: number) => {
    const res = await apiFetch('/calendar/suggest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start, duration_minutes: durationMinutes }),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Contacts (prompt2 Phase P4 — cross-channel person resolution + VIP)
  getContacts: async () => {
    const res = await apiFetch('/contacts')
    return { data: await listFromResponse(res), status: res.status }
  },
  getContact: async (personId: string) => {
    const res = await apiFetch(`/contacts/${personId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },
  ingestContact: async (payload: Record<string, unknown>) => {
    const res = await apiFetch('/contacts/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  setContactVip: async (personId: string, vip: boolean) => {
    const res = await apiFetch(`/contacts/${personId}/vip`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vip }),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Waiting-On (prompt2 §19 — Waiting-On Detection / §31)
  getWaitingOn: async (status?: string) => {
    const qs = status ? new URLSearchParams({ status }).toString() : ''
    const res = await apiFetch(`/waiting-on${qs ? `?${qs}` : ''}`)
    return { data: await listFromResponse(res), status: res.status }
  },
  getWaitingOnItem: async (requestId: string) => {
    const res = await apiFetch(`/waiting-on/${requestId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },
  ingestWaitingOn: async (payload: Record<string, unknown>) => {
    const res = await apiFetch('/waiting-on/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  resolveWaitingOn: async (requestId: string, note?: string | null) => {
    const res = await apiFetch(`/waiting-on/${requestId}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: note ?? null }),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  followUpWaitingOn: async (requestId: string) => {
    const res = await apiFetch(`/waiting-on/${requestId}/follow-up`, { method: 'POST' })
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Models
  // Models
  getModels: async () => {
    const res = await apiFetch('/models')
    return { data: await listFromResponse(res), status: res.status }
  },
  getModel: async (modelId: string) => {
    const res = await apiFetch(`/models/${modelId}`)
    return { data: await responseOrThrow(res), status: res.status }
  },

  // Auth (mock stub)
  login: async (username: string, password: string) => {
    const res = await apiFetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    return { data: await responseOrThrow(res), status: res.status }
  },
  logout: async () => {
    const res = await apiFetch('/auth/logout', { method: 'POST' })
    return { data: await responseOrThrow(res), status: res.status }
  },
  // Briefing (prompt2 §30 — the one-screen daily brief)
  getBriefing: async (kind: BriefingKind = 'morning') => {
    const res = await apiFetch(`/briefing?kind=${kind}`)
    return { data: (await responseOrThrow(res)) as Briefing, status: res.status }
  },
  me: async () => {
    const res = await apiFetch('/auth/me')
    return { data: await responseOrThrow(res), status: res.status }
  },
}

// ─── Mock API implementation ──────────────────────────────────────────────────

const mockApi = {
  // Health & Overview
  getOverview: async () => {
    await delay(80)
    return { data: mockOverview, status: 200 }
  },
  getHealth: async () => {
    await delay(50)
    return { data: mockOverview.health, status: 200 }
  },

  // Tasks (Task DAG with 8 states)
  getTasks: async () => {
    await delay(100)
    return { data: mockTasks, status: 200 }
  },
  getTask: async (taskId: string) => {
    await delay(50)
    const task = mockTasks.find((t) => t.task_id === taskId)
    return { data: task, status: task ? 200 : 404 }
  },

  // Agents
  getAgents: async () => {
    await delay(100)
    return { data: mockAgents, status: 200 }
  },
  getAgent: async (agentId: string) => {
    await delay(50)
    const agent = mockAgents.find((a) => a.id === agentId)
    return { data: agent, status: agent ? 200 : 404 }
  },

  // Compute nodes (for Inference view)
  getComputeNodes: async () => {
    await delay(80)
    return { data: mockComputeNodes, status: 200 }
  },
  getComputeNode: async (nodeId: string) => {
    await delay(50)
    const node = mockComputeNodes.find((n) => n.id === nodeId)
    return { data: node, status: node ? 200 : 404 }
  },

  // OmniRoute routes (for Inference view)
  getRoutes: async () => {
    await delay(80)
    return { data: mockRoutes, status: 200 }
  },
  getRoute: async (routeId: string) => {
    await delay(50)
    const route = mockRoutes.find((r) => r.routeId === routeId)
    return { data: route, status: route ? 200 : 404 }
  },

  // Historical model performance (for Inference view)
  getModelHistory: async () => {
    await delay(80)
    return { data: mockModelHistory, status: 200 }
  },

  // Incidents
  getIncidents: async () => {
    await delay(100)
    return { data: mockIncidents, status: 200 }
  },
  getIncident: async (incidentId: string) => {
    await delay(50)
    const incident = mockIncidents.find((i) => i.id === incidentId)
    return { data: incident, status: incident ? 200 : 404 }
  },

  // Decisions
  getDecisions: async () => {
    await delay(80)
    return { data: mockDecisions, status: 200 }
  },
  getDecision: async (decisionId: string) => {
    await delay(50)
    const decision = mockDecisions.find((d) => d.id === decisionId)
    return { data: decision, status: decision ? 200 : 404 }
  },

  // Events (Timeline)
  getEvents: async () => {
    await delay(100)
    return { data: mockEvents, status: 200 }
  },
  getEvent: async (eventId: string) => {
    await delay(50)
    const event = mockEvents.find((e) => e.id === eventId)
    return { data: event, status: event ? 200 : 404 }
  },

  // Systems health
  getSystems: async () => {
    await delay(80)
    return { data: mockSystems, status: 200 }
  },
  getMetrics: async () => {
    await delay(50)
    return { data: { timestamp: new Date().toISOString() }, status: 200 }
  },

  // Ask endpoint
  postAsk: async (_message: string) => {
    await delay(600)
    return { data: mockAskResponse, status: 200 }
  },

  // Goals
  getGoals: async () => {
    await delay(80)
    return { data: mockGoals, status: 200 }
  },
  getGoal: async (goalId: string) => {
    await delay(50)
    const goal = mockGoals.find((g) => g.id === goalId)
    return { data: goal, status: goal ? 200 : 404 }
  },
  deleteGoal: async (goalId: string) => {
    await delay(50)
    const exists = mockGoals.some((g) => g.id === goalId)
    return { data: null, status: exists ? 204 : 404 }
  },

  // Attention (prompt2 Phase P0 — Personal Operations Plane). Mock returns the
  // same 4 demo items the backend seeds, so the Now screen renders identically
  // in mock and real mode.
  getAttention: async (_status?: string) => {
    await delay(120)
    return { data: mockAttention, status: 200 }
  },
  getAttentionNow: async () => {
    await delay(120)
    return { data: mockAttention, status: 200 }
  },
  createAttention: async (payload: AttentionCreatePayload) => {
    await delay(120)
    const id = `att-mock-${Math.random().toString(16).slice(2, 10)}`
    return {
      data: {
        attention_id: id,
        reason: payload.reason,
        priority: 0.5,
        urgency: 0.5,
        importance: 0.5,
        due: null,
        entities: payload.entities ?? [],
        sources: payload.sources ?? ['mock'],
        suggested_action: payload.suggested_action ?? null,
        status: 'NOW',
        why: ['base 0.25 from importance 0.50 + 0.25 from urgency 0.50'],
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      status: 201,
    }
  },
  attentionAction: async (attentionId: string, action: string) => {
    await delay(60)
    const found = mockAttention.find((a) => a.attention_id === attentionId)
    if (!found) return { data: undefined, status: 404 }
    const statusMap: Record<string, string> = {
      resolve: 'RESOLVED',
      suppress: 'SUPPRESSED',
      acknowledge: 'RESOLVED',
      snooze: 'TODAY',
    }
    return { data: { ...found, status: statusMap[action] ?? found.status }, status: 200 }
  },

  // Messages (prompt2 Phase P1 — Personal Operations Plane)
  getMessages: async (_channel?: string) => {
    await delay(60)
    return { data: mockMessages, status: 200 }
  },
  getMessageThread: async (conversationId: string) => {
    await delay(40)
    const conv = mockMessages.find((c) => c.conversation_id === conversationId)
    if (!conv) return { data: undefined, status: 404 }
    const messages = [
      {
        message_id: 'msg-1',
        conversation_id: conversationId,
        direction: 'INBOUND',
        peer_identifier: conv.peer_identifier,
        peer_name: conv.peer_name,
        body: 'Are you still free to review the deck Friday?',
        channel: conv.channel,
        timestamp: conv.last_timestamp,
        source: 'personal-relay',
      },
    ]
    return { data: { conversation: conv, messages }, status: 200 }
  },
  ingestMessage: async (payload: Record<string, unknown>) => {
    await delay(80)
    return {
      data: {
        message: { message_id: payload.message_id ?? 'mock-msg', conversation_id: 'conv-mock|imessage', direction: payload.direction ?? 'INBOUND', peer_identifier: payload.peer_identifier ?? '+1', peer_name: payload.peer_name ?? null, body: payload.body ?? '', channel: payload.channel ?? 'imessage', timestamp: payload.timestamp ?? new Date().toISOString(), source: 'personal-relay' },
        conversation: { conversation_id: 'conv-mock|imessage', peer_identifier: payload.peer_identifier ?? '+1', peer_name: payload.peer_name ?? null, channel: payload.channel ?? 'imessage', message_count: 1, unread: 1, last_body: payload.body ?? '', last_direction: payload.direction ?? 'INBOUND', last_timestamp: payload.timestamp ?? new Date().toISOString(), attention_id: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        new_attention: null,
        duplicate: false,
      },
      status: 201,
    }
  },
  acknowledgeMessage: async (conversationId: string) => {
    await delay(40)
    const conv = mockMessages.find((c) => c.conversation_id === conversationId)
    if (!conv) return { data: undefined, status: 404 }
    return {
      data: { conversation: { ...conv, unread: 0 }, attention_id: conv.attention_id, resolved: true },
      status: 200,
    }
  },
  draftReply: async (conversationId: string) => {
    await delay(50)
    const conv = mockMessages.find((c) => c.conversation_id === conversationId)
    if (!conv) return { data: undefined, status: 404 }
    return {
      data: {
        conversation_id: conversationId,
        to: conv.peer_name ?? conv.peer_identifier,
        draft: 'Sure — let me check and get back to you.',
        basis: 'mock draft (NERVE_USE_MOCK=true)',
      },
      status: 200,
    }
  },

  // Email (prompt2 Phase P0 — mock transport)
  getEmailThreads: async () => {
    await delay(60)
    return { data: mockEmailThreads, status: 200 }
  },
  getEmailThread: async (threadId: string) => {
    await delay(40)
    const thread = mockEmailThreads.find((t) => t.thread_id === threadId)
    if (!thread) return { data: undefined, status: 404 }
    const messages = [
      {
        message_id: 'email-1',
        thread_id: threadId,
        direction: 'RECEIVED',
        from_address: thread.from_address,
        to_address: thread.to_address,
        subject: thread.subject,
        body: 'Are you still free to review the deck Friday?',
        timestamp: new Date(Date.now() - 20 * 60 * 1000).toISOString(),
        source: 'personal-relay',
      },
      {
        message_id: 'email-2',
        thread_id: threadId,
        direction: 'RECEIVED',
        from_address: thread.from_address,
        to_address: thread.to_address,
        subject: thread.subject,
        body: thread.last_body ?? 'No rush — Monday works too if Friday is gone.',
        timestamp: thread.last_timestamp,
        source: 'personal-relay',
      },
    ]
    return { data: { thread, messages }, status: 200 }
  },
  ingestEmail: async (payload: Record<string, unknown>) => {
    await delay(80)
    return {
      data: {
        message: {
          message_id: payload.message_id ?? 'mock-email',
          thread_id: 'thr-mock|subject',
          direction: payload.direction ?? 'RECEIVED',
          from_address: payload.from_address ?? 'a@example.com',
          to_address: payload.to_address ?? 'me@example.com',
          subject: payload.subject ?? 'Subject',
          body: payload.body ?? '',
          timestamp: payload.timestamp ?? new Date().toISOString(),
          source: 'personal-relay',
        },
        thread: {
          thread_id: 'thr-mock|subject',
          from_address: payload.from_address ?? 'a@example.com',
          to_address: payload.to_address ?? 'me@example.com',
          sender_name: payload.sender_name ?? null,
          subject: payload.subject ?? 'Subject',
          message_count: 1,
          unread: 1,
          last_body: payload.body ?? '',
          last_direction: payload.direction ?? 'RECEIVED',
          last_timestamp: payload.timestamp ?? new Date().toISOString(),
          attention_id: null,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
        new_attention: null,
        duplicate: false,
      },
      status: 201,
    }
  },
  acknowledgeEmail: async (threadId: string) => {
    await delay(40)
    const thread = mockEmailThreads.find((t) => t.thread_id === threadId)
    if (!thread) return { data: undefined, status: 404 }
    return {
      data: { thread: { ...thread, unread: 0 }, attention_id: thread.attention_id, resolved: true },
      status: 200,
    }
  },
  draftEmailReply: async (threadId: string) => {
    await delay(50)
    const thread = mockEmailThreads.find((t) => t.thread_id === threadId)
    if (!thread) return { data: undefined, status: 404 }
    return {
      data: {
        thread_id: threadId,
        to: thread.sender_name ?? thread.from_address,
        draft: 'Sure — let me check and get back to you.',
        basis: 'mock draft (NERVE_USE_MOCK=true)',
      },
      status: 200,
    }
  },

  // Voicemail (prompt2 Phase P2 — mock transport)
  getVoicemailThreads: async () => {
    await delay(60)
    return { data: mockVoicemails, status: 200 }
  },
  getVoicemailThread: async (threadId: string) => {
    await delay(40)
    const thread = mockVoicemails.find((t) => t.thread_id === threadId)
    if (!thread) return { data: undefined, status: 404 }
    const voicemails = [
      {
        voicemail_id: `vm-${threadId}-1`,
        thread_id: threadId,
        caller_phone: thread.caller_phone,
        caller_name: thread.caller_name,
        transcript: thread.last_transcript ?? '',
        duration_seconds: thread.last_duration_seconds ?? 0,
        received_at: thread.last_received_at,
        read: thread.unread === 0,
        needs_callback: thread.needs_callback,
        callback_reason: thread.callback_reason,
        source: 'share-sheet',
      },
    ]
    return { data: { thread, voicemails }, status: 200 }
  },
  ingestVoicemail: async (payload: Record<string, unknown>) => {
    await delay(80)
    const threadId = 'vthr-mock|voicemail'
    return {
      data: {
        voicemail: {
          voicemail_id: payload.voicemail_id ?? 'mock-vm',
          thread_id: threadId,
          caller_phone: payload.caller_phone ?? '+1 000-000-0000',
          caller_name: payload.caller_name ?? null,
          transcript: payload.transcript ?? '',
          duration_seconds: payload.duration_seconds ?? 0,
          received_at: payload.received_at ?? new Date().toISOString(),
          read: false,
          needs_callback: false,
          callback_reason: null,
          source: payload.source ?? 'share-sheet',
        },
        thread: {
          thread_id: threadId,
          caller_phone: payload.caller_phone ?? '+1 000-000-0000',
          caller_name: payload.caller_name ?? null,
          voicemail_count: 1,
          unread: 1,
          last_transcript: payload.transcript ?? '',
          last_duration_seconds: payload.duration_seconds ?? 0,
          last_received_at: payload.received_at ?? new Date().toISOString(),
          needs_callback: false,
          callback_reason: null,
          attention_id: null,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
        new_attention: null,
        duplicate: false,
      },
      status: 201,
    }
  },
  acknowledgeVoicemail: async (threadId: string) => {
    await delay(40)
    const thread = mockVoicemails.find((t) => t.thread_id === threadId)
    if (!thread) return { data: undefined, status: 404 }
    return {
      data: { thread: { ...thread, unread: 0 }, attention_id: thread.attention_id, resolved: true },
      status: 200,
    }
  },

  // Calendar (prompt2 Phase P2 — mock transport)
  getCalendar: async (_params?: { start?: string; end?: string; limit?: number }) => {
    await delay(60)
    return { data: mockCalendar, status: 200 }
  },
  ingestCalendarEvent: async (payload: Record<string, unknown>) => {
    await delay(80)
    return { data: { ...(payload as object), source: payload.source ?? 'calendar-sync' }, status: 201 }
  },
  calendarAvailability: async (start: string, end: string) => {
    await delay(40)
    const clashes = mockCalendar.filter((ev) => {
      const s = new Date(ev.start ?? 0).getTime()
      const e = new Date(ev.end ?? 0).getTime()
      const ws = new Date(start).getTime()
      const we = new Date(end).getTime()
      return s < we && e > ws
    })
    if (clashes.length === 0) {
      return { data: { available: true, start, end, conflicts: [], earliest_free_after: start }, status: 200 }
    }
    const earliest = new Date(Math.max(...clashes.map((c) => new Date(c.end as string).getTime())))
    return { data: { available: false, start, end, conflicts: clashes, earliest_free_after: earliest.toISOString() }, status: 200 }
  },
  calendarFreeTime: async (start: string, durationMinutes: number) => {
    await delay(40)
    const ws = new Date(start).getTime()
    const dur = durationMinutes * 60_000
    for (let t = ws; t < ws + 7 * 86_400_000; t += 30 * 60_000) {
      const blocked = mockCalendar.some((ev) => {
        const s = new Date(ev.start ?? 0).getTime()
        const e = new Date(ev.end ?? 0).getTime()
        return s < t + dur && e > t
      })
      if (!blocked) return { data: { start, duration_minutes: durationMinutes, free_at: new Date(t).toISOString(), found: true }, status: 200 }
    }
    return { data: { start, duration_minutes: durationMinutes, free_at: null, found: false }, status: 200 }
  },
  calendarSuggest: async (start: string, durationMinutes: number) => {
    await delay(40)
    const end = new Date(new Date(start).getTime() + durationMinutes * 60_000)
    const r = await mockApi.calendarAvailability(start, end.toISOString())
    const d = (r.data as Record<string, unknown>)
    if (d.available) {
      return { data: { ...d, suggested_at: start, suggestion: `Yes — ${start} works.` }, status: 200 }
    }
    const earliest = d.earliest_free_after as string
    return { data: { ...d, suggested_at: earliest, suggestion: `I can do after ${new Date(earliest).toTimeString().slice(0, 5)}.` }, status: 200 }
  },

  // Contacts (prompt2 Phase P4 — mock transport). Mirrors the backend's
  // idempotent upsert + VIP toggle so /contacts behaves the same in mock mode.
  getContacts: async () => {
    await delay(60)
    return { data: mockContacts, status: 200 }
  },
  getContact: async (personId: string) => {
    await delay(40)
    const contact = mockContacts.find((c) => c.person_id === personId)
    if (!contact) return { data: undefined, status: 404 }
    return { data: contact, status: 200 }
  },
  ingestContact: async (payload: Record<string, unknown>) => {
    await delay(80)
    const existing = mockContacts.find((c) => c.person_id === payload.person_id)
    const now = new Date().toISOString()
    const contact = existing
      ? {
          ...existing,
          display_name: (payload.display_name as string) || existing.display_name,
          emails: [...new Set([...existing.emails, ...((payload.emails as string[]) ?? [])])],
          phones: [...new Set([...existing.phones, ...((payload.phones as string[]) ?? [])])],
          is_vip: (payload.is_vip as boolean) ?? existing.is_vip,
          source: (payload.source as string) ?? existing.source,
          last_updated_at: now,
        }
      : {
          person_id: (payload.person_id as string) ?? `person-${Math.random().toString(16).slice(2, 10)}`,
          display_name: (payload.display_name as string) ?? 'New Contact',
          emails: (payload.emails as string[]) ?? [],
          phones: (payload.phones as string[]) ?? [],
          notes: (payload.notes as string | null) ?? null,
          is_vip: (payload.is_vip as boolean) ?? false,
          source: (payload.source as string) ?? 'personal-relay',
          created_at: now,
          last_updated_at: now,
        }
    return { data: { contact, created: !existing }, status: existing ? 200 : 201 }
  },
  setContactVip: async (personId: string, vip: boolean) => {
    await delay(40)
    const contact = mockContacts.find((c) => c.person_id === personId)
    if (!contact) return { data: undefined, status: 404 }
    return { data: { contact: { ...contact, is_vip: vip, last_updated_at: new Date().toISOString() } }, status: 200 }
  },

  // Waiting-On (prompt2 §19 — mock transport)
  getWaitingOn: async (status?: string) => {
    await delay(60)
    const items =
      status && status !== 'all'
        ? mockWaitingOn.filter((w) => w.status === status)
        : mockWaitingOn
    // Longest-waiting first (requested_at ascending), matching the backend.
    return {
      data: [...items].sort((a, b) => a.requested_at.localeCompare(b.requested_at)),
      status: 200,
    }
  },
  getWaitingOnItem: async (requestId: string) => {
    await delay(40)
    const item = mockWaitingOn.find((w) => w.request_id === requestId)
    if (!item) return { data: undefined, status: 404 }
    return { data: item, status: 200 }
  },
  ingestWaitingOn: async (payload: Record<string, unknown>) => {
    await delay(80)
    const requestId = (payload.request_id as string) ?? `wo-mock-${Math.random().toString(16).slice(2, 10)}`
    const existing = mockWaitingOn.find((w) => w.request_id === requestId)
    if (existing) return { data: { item: existing, new_attention: null, duplicate: true }, status: 200 }
    const now = new Date().toISOString()
    const item = {
      request_id: requestId,
      person_display_name: (payload.person_display_name as string) ?? 'Someone',
      person_id: (payload.person_id as string | null) ?? null,
      description: (payload.description as string) ?? '',
      requested_at: (payload.requested_at as string) ?? now,
      source: (payload.source as string) ?? 'messages',
      source_thread_id: (payload.source_thread_id as string | null) ?? null,
      status: 'open',
      attention_id: null,
      follow_up_count: 0,
      last_follow_up_at: null,
      resolved_at: null,
      note: null,
      created_at: now,
      updated_at: now,
      // Echo the relay's escalation policy, mirroring the backend ingest
      // (which stores whatever the relay posts; default null).
      escalation_policy: (payload.escalation_policy as Record<string, unknown> | null) ?? null,
    }
    mockWaitingOn.unshift(item)
    return { data: { item, new_attention: null, duplicate: false }, status: 201 }
  },
  resolveWaitingOn: async (requestId: string, note?: string | null) => {
    await delay(40)
    const item = mockWaitingOn.find((w) => w.request_id === requestId)
    if (!item) return { data: undefined, status: 404 }
    const now = new Date().toISOString()
    item.status = 'resolved'
    item.resolved_at = now
    item.note = note ?? item.note
    item.updated_at = now
    return { data: { item, attention_id: item.attention_id, resolved: true }, status: 200 }
  },
  followUpWaitingOn: async (requestId: string) => {
    await delay(50)
    const item = mockWaitingOn.find((w) => w.request_id === requestId)
    if (!item) return { data: undefined, status: 404 }
    const now = new Date().toISOString()
    item.follow_up_count += 1
    item.last_follow_up_at = now
    item.updated_at = now
    const days = Math.floor(
      (Date.now() - new Date(item.requested_at).getTime()) / (24 * 60 * 60 * 1000),
    )
    const first = item.person_display_name.split(' ')[0]
    const timing = days <= 0 ? '' : ` (${days} day${days === 1 ? '' : 's'}, no reply yet)`
    const draft = `Hi ${first} — following up on ${item.description}${timing}. Any update?`
    return {
      data: {
        request_id: item.request_id,
        draft,
        follow_up_count: item.follow_up_count,
        last_follow_up_at: item.last_follow_up_at,
        status: item.status,
        draft_only: true,
        basis: 'rule-based follow-up draft (never sent; §13)',
        new_attention: null,
      },
      status: 200,
    }
  },

  // Models
  // Models
  getModels: async () => {
    await delay(80)
    return { data: mockModels, status: 200 }
  },
  getModel: async (modelId: string) => {
    await delay(50)
    const model = mockModels.find((m) => m.id === modelId)
    return { data: model, status: model ? 200 : 404 }
  },

  // Auth (mock stub)
  login: async (_username: string, _password: string) => {
    await delay(200)
    return {
      data: {
        access_token: 'mock-jwt-token',
        user: { username: 'admin', roles: ['admin'] },
      },
      status: 200,
    }
  },
  logout: async () => {
    await delay(50)
    return { data: { success: true }, status: 200 }
  },
  // Briefing (prompt2 §30 — the one-screen daily brief)
  getBriefing: async (kind: BriefingKind = 'morning') => {
    await delay(60)
    return {
      data: {
        kind,
        generated_at: new Date().toISOString(),
        actions: [
          { attention_id: 'att-demo-call-pharmacy', label: 'Call the pharmacy before their 5 PM cutoff', due: null },
        ],
        waiting_on: [
          { request_id: 'wo-demo-sam', person: 'Sam', description: 'Deployment credentials', days_elapsed: 3 },
        ],
        today: [
          { event_id: 'cal-demo-standup', title: 'Standup', start: new Date().toISOString() },
        ],
        overnight: { emails: 2, messages: 1, voicemails: 0 },
        empty: false,
      },
      status: 200,
    }
  },
  me: async () => {
    await delay(50)
    return { data: { username: 'admin', roles: ['admin'] }, status: 200 }
  },
}

// ─── Export unified API client ────────────────────────────────────────────────

const api = useMockMode ? mockApi : realApi

// ─── Export typed API clients ─────────────────────────────────────────────────

export const overviewApi = {
  get: () => api.getOverview(),
  getHealth: () => api.getHealth(),
}

export const tasksApi = {
  list: () => api.getTasks(),
  get: (taskId: string) => api.getTask(taskId),
}

export const agentsApi = {
  list: () => api.getAgents(),
  get: (agentId: string) => api.getAgent(agentId),
}

export const computeApi = {
  list: () => api.getComputeNodes(),
  get: (nodeId: string) => api.getComputeNode(nodeId),
}

export const routesApi = {
  list: () => api.getRoutes(),
  get: (routeId: string) => api.getRoute(routeId),
}

export const modelHistoryApi = {
  list: () => api.getModelHistory(),
}

export const incidentsApi = {
  list: () => api.getIncidents(),
  get: (incidentId: string) => api.getIncident(incidentId),
}

export const decisionsApi = {
  list: () => api.getDecisions(),
  get: (decisionId: string) => api.getDecision(decisionId),
}

export const eventsApi = {
  list: () => api.getEvents(),
  get: (eventId: string) => api.getEvent(eventId),
}

export const systemsApi = {
  health: () => api.getSystems(),
  metrics: () => api.getMetrics(),
}

export const askApi = {
  post: (message: string) => api.postAsk(message),
}

export const goalsApi = {
  list: () => api.getGoals(),
  get: (goalId: string) => api.getGoal(goalId),
  delete: (goalId: string) => api.deleteGoal(goalId),
}

export const modelsApi = {
  list: () => api.getModels(),
  get: (modelId: string) => api.getModel(modelId),
}

export const attentionApi = {
  list: (status?: string) => api.getAttention(status),
  now: () => api.getAttentionNow(),
  create: (payload: Parameters<typeof api.createAttention>[0]) =>
    api.createAttention(payload),
  action: (
    attentionId: string,
    action: 'resolve' | 'suppress' | 'acknowledge' | 'snooze',
  ) => api.attentionAction(attentionId, action),
}

export const messagesApi = {
  list: (channel?: string) => api.getMessages(channel),
  thread: (conversationId: string) => api.getMessageThread(conversationId),
  ingest: (payload: Record<string, unknown>) => api.ingestMessage(payload),
  acknowledge: (conversationId: string) => api.acknowledgeMessage(conversationId),
  draft: (conversationId: string) => api.draftReply(conversationId),
}

export const emailApi = {
  list: () => api.getEmailThreads(),
  thread: (threadId: string) => api.getEmailThread(threadId),
  ingest: (payload: Record<string, unknown>) => api.ingestEmail(payload),
  acknowledge: (threadId: string) => api.acknowledgeEmail(threadId),
  draft: (threadId: string) => api.draftEmailReply(threadId),
}

export const voicemailApi = {
  list: () => api.getVoicemailThreads(),
  thread: (threadId: string) => api.getVoicemailThread(threadId),
  ingest: (payload: Record<string, unknown>) => api.ingestVoicemail(payload),
  acknowledge: (threadId: string) => api.acknowledgeVoicemail(threadId),
}

export const calendarApi = {
  list: (params?: { start?: string; end?: string; limit?: number }) => api.getCalendar(params),
  ingest: (payload: Record<string, unknown>) => api.ingestCalendarEvent(payload),
  availability: (start: string, end: string) => api.calendarAvailability(start, end),
  freeTime: (start: string, durationMinutes: number, horizonMinutes?: number) =>
    api.calendarFreeTime(start, durationMinutes, horizonMinutes),
  suggest: (start: string, durationMinutes: number) => api.calendarSuggest(start, durationMinutes),
}

export const contactsApi = {
  list: () => api.getContacts(),
  person: (personId: string) => api.getContact(personId),
  ingest: (payload: Record<string, unknown>) => api.ingestContact(payload),
  setVip: (personId: string, vip: boolean) => api.setContactVip(personId, vip),
}

export const waitingOnApi = {
  list: (status?: string) => api.getWaitingOn(status),
  item: (requestId: string) => api.getWaitingOnItem(requestId),
  ingest: (payload: Record<string, unknown>) => api.ingestWaitingOn(payload),
  resolve: (requestId: string, note?: string | null) => api.resolveWaitingOn(requestId, note),
  followUp: (requestId: string) => api.followUpWaitingOn(requestId),
}

// Briefing (prompt2 §30 — the one-screen daily brief)
export const briefingApi = {
  compose: (kind: BriefingKind = 'morning') => api.getBriefing(kind),
}

export const authApi = {
  login: (username: string, password: string) => api.login(username, password),
  logout: () => api.logout(),
  me: () => api.me(),
}

// Re-export for backward compatibility
export { api }
