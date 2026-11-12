/**
 * MailSecurityIndicator — the NerveShield safety card (Task 1.11, extended
 * by Task 7.1).
 *
 * A read-only security card for one message. It loads one redacted
 * assessment, either by assessment id or (Task 7.1) by the committed mail
 * communication id, and shows (R-020.1):
 *
 *   - the disposition, the SHADOW / observation-only context, and an
 *     evidence-fidelity badge (how much of the original message the verdict
 *     was computed from),
 *   - the "why" (reason codes),
 *   - structured provenance: email-authentication results, each with where
 *     the result came from (independent check, provider-reported, untrusted
 *     header, unknown),
 *   - structured explanation: each finding's rule, severity, status and
 *     explanation code,
 *   - confidence semantics per axis (calibrated / uncalibrated estimate /
 *     NOT_EVALUATED), never a raw score shown as a probability,
 *   - NOT_EVALUATED badges for every check that did not run or is missing.
 *
 * Security invariants (stricter wins):
 *
 * - SAFE RENDERING (R-020.3, T-05, T-11). No remote resource of any kind (no
 *   <img>, background URL, <iframe>, <script>, web font or prefetch) and NO
 *   `dangerouslySetInnerHTML`. Every string from the assessment is rendered
 *   as inert, React-escaped text. The only request this component makes is
 *   one same-origin GET to the mail-security read API.
 *
 * - READING != AUTHORIZING (R-020.4, T-26). Display only: no action control,
 *   no "trust this sender". The card states that reviewing the message,
 *   independently verifying the sender, and approving an action are three
 *   separate things, and that none of them happens here.
 *
 * - FAIL CLOSED. No assessment, a safe 401/403/404, mock mode or an error
 *   all render the neutral "Not assessed" state; the host view never breaks.
 *
 * - ACCESSIBILITY. State is carried by text + an inert glyph + an accessible
 *   name on a `role="status"` region, never by color alone. Details sit in a
 *   native <details> disclosure (keyboard operable by default).
 */

import { useEffect, useState, type ReactNode } from 'react'
import {
  mailSecurityApi,
  type AssessmentDetail,
  type AssessmentFinding,
  type AuthenticationResult,
  type AxisView,
  type Disposition,
} from '../api/mailSecurity'
import { MailSecurityFeedback } from './MailSecurityFeedback'

/** Exactly one lookup key is used; with neither, nothing is fetched. */
export interface MailSecurityIndicatorProps {
  /** Fetch by assessment id (Task 1.11). */
  assessmentId?: string | null
  /** Fetch the newest assessment for a mail communication (Task 7.1). */
  communicationId?: string | null
}

interface DispositionStyle {
  label: string
  description: string
  glyph: string
  className: string
}

const DISPOSITION_STYLES: Record<Disposition, DispositionStyle> = {
  SAFE_TO_PROCESS: {
    label: 'Safe to process',
    description: 'No blocking signals found.',
    glyph: '✓',
    className:
      'border-green-300 dark:border-green-700 bg-green-50 dark:bg-green-900/30 text-green-800 dark:text-green-200',
  },
  LOW_PRIORITY: {
    label: 'Low priority',
    description: 'Clean but routine — no action expected.',
    glyph: '•',
    className:
      'border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/60 text-gray-700 dark:text-gray-200',
  },
  REVIEW: {
    label: 'Review',
    description: 'Worth a look before you act.',
    glyph: '?',
    className:
      'border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200',
  },
  RESTRICTED: {
    label: 'Restricted',
    description: 'Strong caution — verify the sender independently.',
    glyph: '!',
    className:
      'border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300',
  },
  INCOMPLETE: {
    label: 'Incomplete',
    description: 'Could not be fully assessed — treat with caution.',
    glyph: '…',
    className:
      'border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200',
  },
  UNKNOWN: {
    label: 'Not assessed',
    description: 'No security assessment on record for this message.',
    glyph: '–',
    className:
      'border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/60 text-muted',
  },
}

/** Evidence fidelity → short plain-language label (unknown values shown raw). */
const FIDELITY_LABELS: Record<string, string> = {
  ORIGINAL_RFC822: 'Full original message',
  PROVIDER_FULL: 'Full provider copy',
  SELECTED_HEADERS_AND_BODY: 'Some headers + body',
  BODY_ONLY: 'Body text only',
  METADATA_ONLY: 'Metadata only',
  UNKNOWN: 'Source fidelity unknown',
}

/** Where an authentication result came from (R-020.1 provenance). */
const PROVENANCE_LABELS: Record<string, string> = {
  INDEPENDENT: 'checked independently',
  PROVIDER_REPORTED: 'reported by mail provider',
  UNTRUSTED_HEADER: 'from an untrusted header',
  UNKNOWN: 'source unknown',
}

/**
 * Task 7.2 (R-013.4): fixed advice text per server code. Shown even when the
 * sender authenticates, because a real sender can still make a bad request.
 */
const ADVICE_TEXT: Record<string, string> = {
  VERIFY_PAYMENT_REQUEST:
    'Before paying or changing bank details, confirm with the sender using a phone number or site you already know. Do not use contact details from this message.',
  VERIFY_CREDENTIAL_REQUEST:
    'Do not sign in or share codes from a link in this message. Go to the site directly by typing its address.',
  VERIFY_HIGH_IMPACT_REQUEST:
    'This asks for money, credentials or personal data. Confirm through a channel you already trust before acting.',
  VERIFY_SENDER_IDENTITY:
    'The sender may not be who they claim. Check their address and contact them another way if unsure.',
}

function toDisposition(value: string | null | undefined): Disposition {
  if (value && value in DISPOSITION_STYLES) return value as Disposition
  return 'UNKNOWN'
}

/** Small inert badge. Text only; never markup. */
function Badge({ children, testId }: { children: string; testId?: string }) {
  return (
    <span
      data-testid={testId}
      className="rounded-full border border-current/40 px-2 py-0.5 text-xs font-medium"
    >
      {children}
    </span>
  )
}

function NotEvaluated() {
  return (
    <span
      data-testid="mail-security-not-evaluated"
      className="rounded border border-dashed border-current/50 px-1 text-[10px] font-semibold uppercase"
    >
      NOT_EVALUATED
    </span>
  )
}

function CodeChips({ title, codes }: { title: string; codes: string[] }) {
  if (codes.length === 0) return null
  return (
    <div className="mt-1.5">
      <p className="text-xs font-semibold uppercase tracking-wide opacity-80">
        {title}
      </p>
      <ul className="mt-0.5 flex flex-wrap gap-1">
        {codes.map((code) => (
          <li
            key={code}
            className="rounded bg-black/5 dark:bg-white/10 px-1.5 py-0.5 text-xs font-mono"
          >
            {code}
          </li>
        ))}
      </ul>
    </div>
  )
}

function SectionTitle({ children }: { children: string }) {
  return (
    <h4 className="mt-2 text-xs font-semibold uppercase tracking-wide opacity-80">
      {children}
    </h4>
  )
}

/** Authentication results with provenance; missing/UNKNOWN → NOT_EVALUATED. */
function AuthenticationTable({
  authentication,
}: {
  authentication: Record<string, AuthenticationResult>
}) {
  const names = Object.keys(authentication)
  if (names.length === 0) return null
  return (
    <>
      <SectionTitle>Sender authentication</SectionTitle>
      <ul className="mt-0.5 space-y-0.5 text-xs" data-testid="mail-security-auth">
        {names.map((name) => {
          const entry = authentication[name]
          const result = entry?.result ?? null
          const provenance = entry?.provenance ?? null
          const evaluated = result !== null && result !== 'UNKNOWN'
          return (
            <li key={name} className="flex flex-wrap items-center gap-1.5">
              <span className="w-12 font-mono uppercase">{name}</span>
              {evaluated ? (
                <span className="font-mono">{result}</span>
              ) : (
                <NotEvaluated />
              )}
              {provenance && (
                <span className="opacity-80">
                  ({PROVENANCE_LABELS[provenance] ?? provenance})
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </>
  )
}

/** One axis: calibrated value, uncalibrated estimate, or NOT_EVALUATED. */
function AxisLine({ name, axis }: { name: string; axis: AxisView }) {
  const label = name.replace(/_/g, ' ')
  let state: ReactNode
  if (axis.semantics === null || axis.semantics === 'NOT_EVALUATED') {
    state = <NotEvaluated />
  } else if (axis.calibrated && typeof axis.value === 'number') {
    state = <span>{`${Math.round(axis.value * 100)}% (calibrated)`}</span>
  } else {
    state = <span>uncalibrated estimate (not a probability)</span>
  }
  return (
    <li className="flex flex-wrap items-center gap-1.5">
      <span className="capitalize">{label}</span>
      {state}
    </li>
  )
}

function Findings({ findings }: { findings: AssessmentFinding[] }) {
  if (findings.length === 0) return null
  return (
    <>
      <SectionTitle>Findings</SectionTitle>
      <ul className="mt-0.5 space-y-1 text-xs" data-testid="mail-security-findings">
        {findings.map((f) => (
          <li key={f.finding_key} className="rounded bg-black/5 dark:bg-white/10 px-1.5 py-1">
            <span className="font-mono">{f.rule_id}</span>
            <span className="ml-1.5 opacity-80">
              {`severity ${f.severity} · ${f.status}`}
            </span>
            {f.status === 'NOT_EVALUATED' && (
              <span className="ml-1.5">
                <NotEvaluated />
              </span>
            )}
            {f.explanation_code && (
              <span className="ml-1.5 font-mono opacity-80">{f.explanation_code}</span>
            )}
            {f.confidence_type === 'ESTIMATE' && (
              <span className="ml-1.5 opacity-80">(uncalibrated estimate)</span>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}

export function MailSecurityIndicator({
  assessmentId,
  communicationId,
}: MailSecurityIndicatorProps) {
  const [detail, setDetail] = useState<AssessmentDetail | null>(null)
  const [loading, setLoading] = useState<boolean>(false)

  useEffect(() => {
    let cancelled = false
    const request = assessmentId
      ? mailSecurityApi.getAssessment(assessmentId)
      : communicationId
        ? mailSecurityApi.getForCommunication(communicationId)
        : null
    if (request === null) {
      setDetail(null)
      setLoading(false)
      return
    }
    setLoading(true)
    void request
      .then((result) => {
        if (!cancelled) setDetail(result)
      })
      .catch(() => {
        if (!cancelled) setDetail(null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [assessmentId, communicationId])

  const disposition = toDisposition(detail?.disposition)
  const style = DISPOSITION_STYLES[disposition]
  const shadow = detail?.mode === 'SHADOW' || detail === null

  const reasonCodes = detail?.reason_codes ?? []
  const checksNotRun = detail?.completeness?.checks_not_run ?? []
  const missingChecks = detail?.completeness?.missing ?? []
  const skipped = Array.from(new Set([...checksNotRun, ...missingChecks]))
  const fidelity = detail?.evidence_fidelity ?? null
  const axes = detail?.axes ?? {}
  const axisNames = Object.keys(axes)

  const accessibleName = loading
    ? 'Security assessment loading'
    : `Security assessment: ${style.label}${
        shadow ? ' (observation only, not enforced)' : ''
      }`

  const advice = detail?.verification_advice ?? []

  return (
    <div>
    <section
      role="status"
      aria-live="polite"
      aria-label={accessibleName}
      data-testid="mail-security-indicator"
      data-disposition={disposition}
      className={`rounded-lg border px-3 py-2 text-sm ${style.className}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span aria-hidden="true" className="font-semibold">
          {style.glyph}
        </span>
        <span className="font-semibold" data-testid="mail-security-disposition">
          {loading ? 'Checking…' : style.label}
        </span>
        <span className="ml-auto flex flex-wrap gap-1">
          {detail && fidelity && (
            <Badge testId="mail-security-fidelity">
              {FIDELITY_LABELS[fidelity] ?? fidelity}
            </Badge>
          )}
          {shadow && !loading && <Badge>SHADOW</Badge>}
        </span>
      </div>

      {!loading && (
        <>
          <p className="mt-1 text-xs opacity-90">{style.description}</p>
          {shadow && (
            <p className="mt-1 text-xs opacity-80">
              Observation only — reviewing this does not approve or authorize
              anything.
            </p>
          )}

          {advice.length > 0 && (
            <div
              data-testid="mail-security-advice"
              className="mt-2 rounded border border-current/40 bg-white/40 dark:bg-black/20 px-2 py-1.5"
            >
              <p className="text-xs font-semibold">Verify independently</p>
              <ul className="mt-0.5 list-disc pl-4 text-xs">
                {advice.map((code) => (
                  <li key={code} data-advice={code}>
                    {ADVICE_TEXT[code] ?? code}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <CodeChips title="Why" codes={reasonCodes} />

          {skipped.length > 0 && (
            <div className="mt-1.5" data-testid="mail-security-not-run">
              <p className="text-xs font-semibold uppercase tracking-wide opacity-80">
                Checks not run
              </p>
              <ul className="mt-0.5 flex flex-wrap gap-1">
                {skipped.map((code) => (
                  <li
                    key={code}
                    className="flex items-center gap-1 rounded bg-black/5 dark:bg-white/10 px-1.5 py-0.5 text-xs font-mono"
                  >
                    {code}
                    <NotEvaluated />
                  </li>
                ))}
              </ul>
            </div>
          )}

          {detail && (
            <details className="mt-2" data-testid="mail-security-details">
              <summary className="cursor-pointer text-xs font-semibold underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-current rounded">
                Provenance and explanation
              </summary>
              <AuthenticationTable authentication={detail.authentication ?? {}} />
              {axisNames.length > 0 && (
                <>
                  <SectionTitle>Confidence</SectionTitle>
                  <ul className="mt-0.5 space-y-0.5 text-xs" data-testid="mail-security-axes">
                    {axisNames.map((name) => (
                      <AxisLine key={name} name={name} axis={axes[name]} />
                    ))}
                  </ul>
                </>
              )}
              <Findings findings={detail.findings ?? []} />
              <SectionTitle>What this card does not do</SectionTitle>
              <ul className="mt-0.5 list-disc pl-4 text-xs opacity-90">
                <li>Reviewing the message is only reading it.</li>
                <li>Verifying the sender means contacting them through a channel you already trust.</li>
                <li>Approving an action is a separate, explicit step. Nothing here approves anything.</li>
              </ul>
            </details>
          )}
        </>
      )}
    </section>
    {/* Task 7.2: feedback sits outside the display-only status region. */}
    {!loading && detail && <MailSecurityFeedback assessmentId={detail.assessment_id} />}
    </div>
  )
}
