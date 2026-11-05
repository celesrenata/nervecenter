/**
 * NerveShield read-only mail-security assessments API client (Phase 1, Task
 * 1.11 — the UI consumer of the Task 1.10 read surface).
 *
 * Typed client for the two GET routes under the `/api/mail-security` prefix
 * (backend/api/endpoints/mail_security.py):
 *
 *   GET /api/mail-security/assessments
 *       ?account_id=&disposition=&cursor=&limit=   (MAX_PAGE_SIZE = 100)
 *     → { data: AssessmentSummary[], meta, next_cursor }
 *   GET /api/mail-security/assessments/{assessment_id}
 *     → AssessmentDetail (summary + authentication + axes + completeness +
 *                         findings; R-020.1 "checks not run")
 *
 * Hard contract (requirements/design/threat-model — stricter behavior wins):
 *
 * - Read-only. This client issues GET only — no writes, no mail mutation, no
 *   feedback (feedback is a later task). Reviewing a verdict is not
 *   authorizing anything (R-020.4, T-26).
 *
 * - Redacted metadata only. The backend already projects an explicit
 *   allow-list of safe metadata + opaque refs (R-017.2, T-14): disposition,
 *   reason/explanation codes, severities, confidence *semantics* + calibration
 *   status, provenance labels, not-evaluated/`checks_not_run` statuses. There
 *   is no raw body / subject / address field in these types, and this client
 *   adds none.
 *
 * - Authorized transport, no new auth path. Every call goes through the shared
 *   `apiFetch`, which attaches the session bearer (`nc.accessToken`) exactly
 *   like the other authorized clients (communications.ts / today.ts). No
 *   credential is sent anywhere new; only the Task 1.10 endpoints are called.
 *
 * - Fail-closed degradation. The Task 1.10 API is deny-by-default today
 *   (production tokens do not yet mint the `account_scopes` claim it requires —
 *   a known, later-task dependency), so it may answer with an empty list or a
 *   403/404. In mock mode (no `VITE_API_BASE`) there is no backend at all.
 *   Both the mock client and the view's caller treat "no assessment" as a
 *   neutral, not-assessed state — never a fabricated verdict, never a thrown
 *   error that breaks the host view.
 */

import { apiFetch, apiErrorFromResponse, useMockMode } from './client'

// ─── Wire types (exact redacted projections from the Task 1.10 API) ──────────

/**
 * Policy disposition — the restrictive SHADOW verdict, never a capability.
 * Mirrors `backend/mail_security/models.Disposition`. `UNKNOWN` is a
 * UI-only state for "no assessment on record" (not a backend value); it is
 * kept distinct so the indicator can say "not assessed" rather than imply a
 * clean verdict.
 */
export type Disposition =
  | 'SAFE_TO_PROCESS'
  | 'LOW_PRIORITY'
  | 'REVIEW'
  | 'RESTRICTED'
  | 'INCOMPLETE'
  | 'UNKNOWN'

/** Redacted list-row view (safe scalar metadata only — no raw content). */
export interface AssessmentSummary {
  assessment_id: string
  account_scope: string
  disposition: string
  mode: string
  processing_state: string
  evidence_fidelity: string
  policy_version: string
  analysis_profile_version: string
  created_at: string | null
}

/** One redacted finding — opaque refs + codes, never a raw probability. */
export interface AssessmentFinding {
  finding_key: string
  rule_id: string
  category: string
  severity: string
  status: string
  confidence_semantics: string | null
  confidence_type: 'ABSENT' | 'ESTIMATE'
  explanation_code: string | null
  evidence_refs: string[]
}

/** One email-authentication result + its provenance label. */
export interface AuthenticationResult {
  result: string | null
  provenance: string | null
}

/** One redacted trust axis — semantics + calibration status only. */
export interface AxisView {
  semantics: string | null
  calibrated: boolean
  /** Only present when calibrated; never a raw/uncalibrated probability. */
  value: number | null
}

/** Completeness: which checks ran, which are missing, which were NOT run. */
export interface Completeness {
  available: string[]
  missing: string[]
  /** R-020.1 "unavailable checks" — checks that were not performed. */
  checks_not_run: string[]
}

/** The full redacted detail view (summary + the Task 1.10 detail fields). */
export interface AssessmentDetail extends AssessmentSummary {
  reason_codes: string[]
  human_approval_required: boolean
  action_capabilities: string[]
  authentication: Record<string, AuthenticationResult>
  axes: Record<string, AxisView>
  completeness: Completeness
  findings: AssessmentFinding[]
}

/** The list envelope: redacted rows + opaque continuation cursor. */
export interface AssessmentListResponse {
  data: AssessmentSummary[]
  meta: Record<string, unknown>
  next_cursor: string | null
}

/** Optional list scoping/paging params (names mirror the backend route). */
export interface AssessmentListParams {
  accountId?: string
  disposition?: string
  cursor?: string
  /** Clamped server-side to MAX_PAGE_SIZE (100). */
  limit?: number
}

export interface MailSecurityClient {
  /**
   * List redacted assessments for the caller's authorized account(s). Fails
   * closed: resolves to an empty list (never throws, never fabricates) when
   * the caller has no authorized scope or the durable plane is not wired.
   */
  listAssessments(params?: AssessmentListParams): Promise<AssessmentListResponse>
  /**
   * Fetch one redacted assessment + findings + checks-not-run. Resolves to
   * `null` on a safe 403/404 (not-yours / absent / deny-by-default) so the
   * view renders a neutral "not assessed" state rather than an error.
   */
  getAssessment(assessmentId: string): Promise<AssessmentDetail | null>
}

// ─── Real API implementation ─────────────────────────────────────────────────

function buildQuery(params: AssessmentListParams): string {
  const qs = new URLSearchParams()
  if (params.accountId) qs.set('account_id', params.accountId)
  if (params.disposition) qs.set('disposition', params.disposition)
  if (params.cursor) qs.set('cursor', params.cursor)
  if (params.limit !== undefined) qs.set('limit', String(params.limit))
  const s = qs.toString()
  return s ? `?${s}` : ''
}

const emptyList = (): AssessmentListResponse => ({
  data: [],
  meta: {},
  next_cursor: null,
})

export const realMailSecurityApi: MailSecurityClient = {
  listAssessments: async (params = {}) => {
    const res = await apiFetch(`/api/mail-security/assessments${buildQuery(params)}`)
    // Deny-by-default today: a forbidden/absent response is not an error the
    // indicator should surface — degrade to an honest empty list so the host
    // view renders exactly as it does now (SHADOW, not-assessed).
    if (res.status === 401 || res.status === 403 || res.status === 404) {
      return emptyList()
    }
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as AssessmentListResponse
  },

  getAssessment: async (assessmentId: string) => {
    const res = await apiFetch(
      `/api/mail-security/assessments/${encodeURIComponent(assessmentId)}`,
    )
    // Safe 404 (absent OR belongs to another account OR deny-by-default) and
    // 401/403 both mean "nothing to show" — resolve to null, never throw.
    if (res.status === 401 || res.status === 403 || res.status === 404) {
      return null
    }
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as AssessmentDetail
  },
}

// ─── Mock mode: honest empty (no backend → nothing assessed) ──────────────────
//
// With no `VITE_API_BASE` there is no Task 1.10 backend, so the client
// degrades honestly: no assessments, no detail — never a fabricated verdict.

export const mockMailSecurityApi: MailSecurityClient = {
  listAssessments: async () => emptyList(),
  getAssessment: async () => null,
}

// ─── Exported client (mock-aware, mirrors communicationsApi / todayApi) ───────

export const mailSecurityApi = useMockMode
  ? mockMailSecurityApi
  : realMailSecurityApi
