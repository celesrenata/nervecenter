/**
 * MailSecurityIndicator — NerveShield Phase 1, Task 1.11.
 *
 * A small, read-only security indicator for a single email thread, embedded in
 * the existing Email view. It consumes the Task 1.10 read-only API
 * (`src/api/mailSecurity.ts`) and surfaces, for one assessment:
 *
 *   - the SHADOW-mode context (the system is observing, not enforcing),
 *   - the safety disposition (SAFE_TO_PROCESS / LOW_PRIORITY / REVIEW /
 *     RESTRICTED / INCOMPLETE, plus the UI-only UNKNOWN / "not assessed"),
 *   - a compact "why" (reason codes) and the checks that were skipped / not run
 *     (R-020.1 unavailable checks).
 *
 * Security invariants this component is responsible for (stricter wins):
 *
 * - SAFE RENDERING (R-020.3, T-05, T-11). Renders NO external/remote resource:
 *   no remote <img>, no background-image URL, no <iframe>, no <script>, no
 *   web-font fetch, no link prefetch. There is NO `dangerouslySetInnerHTML`
 *   anywhere here. Every string sourced from the assessment (disposition,
 *   reason/explanation codes, provenance labels, injection-indicator text) is
 *   rendered as INERT, React-escaped text — never interpreted as HTML and
 *   never executed (T-11). The only network request this component can cause
 *   is the single authorized same-origin XHR to the Task 1.10 API.
 *
 * - READING != AUTHORIZING (R-020.4, T-26). This is display-only. It renders
 *   no action button: nothing here can move / mark / delete mail, approve an
 *   action, or trust a sender. There is deliberately no "trust this sender"
 *   control (design §12).
 *
 * - SHADOW / fail-closed (design §12/§8.2). SHADOW mode is stated explicitly.
 *   When the API returns nothing — deny-by-default (no `account_scopes`
 *   claim yet), feature produced no data, or mock mode — the component renders
 *   a neutral "not assessed" badge and never throws, so the host view looks
 *   exactly as it does today.
 *
 * - ACCESSIBILITY. The disposition is conveyed by text + an inert glyph +ARIA
 *   (an accessible name on a `role="status"` region), never by color alone.
 */

import { useEffect, useState } from 'react'
import {
  mailSecurityApi,
  type AssessmentDetail,
  type Disposition,
} from '../api/mailSecurity'

/** Props: the assessment id to display, or null/undefined when none is known. */
export interface MailSecurityIndicatorProps {
  /**
   * The id of the assessment to show. When null/undefined the component
   * renders the neutral "not assessed" state (no network request is made).
   */
  assessmentId?: string | null
}

/**
 * Per-disposition presentation: an accessible label, a short calm description,
 * and an inert text glyph. Color is paired with text + glyph (never color
 * alone). The classes are static Tailwind utility strings — no URL, no remote
 * font, no background-image — so rendering causes zero network activity.
 */
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
    description: 'No security assessment on record for this thread.',
    glyph: '–',
    className:
      'border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/60 text-muted',
  },
}

/** Normalize a backend disposition string to a known key (UNKNOWN fallback). */
function toDisposition(value: string | null | undefined): Disposition {
  if (value && value in DISPOSITION_STYLES) return value as Disposition
  return 'UNKNOWN'
}

/**
 * Render a code/label list as inert chips. Each entry is plain React text, so
 * it is escaped by React and never interpreted as HTML (T-11): even a
 * prompt-injection-indicator string is shown literally, never executed.
 */
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

export function MailSecurityIndicator({
  assessmentId,
}: MailSecurityIndicatorProps) {
  const [detail, setDetail] = useState<AssessmentDetail | null>(null)
  const [loading, setLoading] = useState<boolean>(false)

  useEffect(() => {
    let cancelled = false
    if (!assessmentId) {
      setDetail(null)
      setLoading(false)
      return
    }
    setLoading(true)
    // getAssessment fails closed: it resolves to null (never throws) on a safe
    // 401/403/404 or in mock mode, so this never breaks the host view.
    void mailSecurityApi
      .getAssessment(assessmentId)
      .then((result) => {
        if (!cancelled) setDetail(result)
      })
      .catch(() => {
        // Defense in depth: any unexpected error degrades to not-assessed.
        if (!cancelled) setDetail(null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [assessmentId])

  const disposition = toDisposition(detail?.disposition)
  const style = DISPOSITION_STYLES[disposition]
  const shadow = detail?.mode === 'SHADOW' || detail === null

  // Reason codes (the "why") and the skipped / not-run checks (R-020.1). Both
  // are opaque code strings from the redacted API, rendered as inert text.
  const reasonCodes = detail?.reason_codes ?? []
  const checksNotRun = detail?.completeness?.checks_not_run ?? []
  const missingChecks = detail?.completeness?.missing ?? []
  const skipped = Array.from(new Set([...checksNotRun, ...missingChecks]))

  const accessibleName = loading
    ? 'Security assessment loading'
    : `Security assessment: ${style.label}${
        shadow ? ' (observation only, not enforced)' : ''
      }`

  return (
    <section
      role="status"
      aria-live="polite"
      aria-label={accessibleName}
      data-testid="mail-security-indicator"
      data-disposition={disposition}
      className={`rounded-lg border px-3 py-2 text-sm ${style.className}`}
    >
      <div className="flex items-center gap-2">
        {/* Inert glyph — decorative; the accessible name carries the state. */}
        <span aria-hidden="true" className="font-semibold">
          {style.glyph}
        </span>
        <span className="font-semibold" data-testid="mail-security-disposition">
          {loading ? 'Checking…' : style.label}
        </span>
        {shadow && !loading && (
          <span
            className="ml-auto rounded-full border border-current/40 px-2 py-0.5 text-xs font-medium"
            title="Observation only — the system does not act on this"
          >
            SHADOW
          </span>
        )}
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

          <CodeChips title="Why" codes={reasonCodes} />

          {skipped.length > 0 && (
            <CodeChips title="Checks not run" codes={skipped} />
          )}
        </>
      )}
    </section>
  )
}
