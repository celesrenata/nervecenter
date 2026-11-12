/**
 * MailSecurityFeedback — NerveShield Task 7.2 reviewer feedback.
 *
 * Four typed verdicts for one assessment: Safe, Suspicious, Not relevant,
 * Wrong intent/category. Codes only: there is no free-text box, so no
 * message content or personal note is ever sent.
 *
 * Feedback is NOT trust. Submitting "Safe" does not allowlist the sender,
 * change the verdict, move mail or feed a model. The backend records the
 * verdict and any follow-up as a proposal a person must review; this
 * component says so after every submission.
 *
 * Deliberately rendered outside the safety card's `role="status"` region so
 * the card itself stays display-only (R-020.4).
 */

import { useEffect, useId, useState } from 'react'
import { mailSecurityApi, type ReviewerVerdict } from '../api/mailSecurity'

const OPTIONS: { verdict: ReviewerVerdict; label: string }[] = [
  { verdict: 'SAFE', label: 'Safe' },
  { verdict: 'SUSPICIOUS', label: 'Suspicious' },
  { verdict: 'NOT_RELEVANT', label: 'Not relevant' },
  { verdict: 'WRONG_INTENT', label: 'Wrong intent or category' },
]

function newKey(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === 'function') return c.randomUUID().replace(/-/g, '')
  const bytes = new Uint8Array(16)
  if (c) c.getRandomValues(bytes)
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

export function MailSecurityFeedback({ assessmentId }: { assessmentId: string }) {
  const [current, setCurrent] = useState<ReviewerVerdict | null>(null)
  const [busy, setBusy] = useState<ReviewerVerdict | null>(null)
  const [message, setMessage] = useState<string>('')
  const [failed, setFailed] = useState(false)
  const legendId = useId()

  useEffect(() => {
    let cancelled = false
    setCurrent(null)
    setMessage('')
    void mailSecurityApi
      .listFeedback(assessmentId)
      .then((rows) => {
        const mine = rows.filter((r) => r.mine)
        if (!cancelled && mine.length > 0) setCurrent(mine[mine.length - 1].verdict)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [assessmentId])

  const submit = async (verdict: ReviewerVerdict) => {
    setBusy(verdict)
    setFailed(false)
    setMessage('')
    try {
      await mailSecurityApi.submitFeedback(assessmentId, verdict, newKey())
      setCurrent(verdict)
      setMessage(
        'Recorded. Nothing was trusted or changed; any follow-up waits for review.',
      )
    } catch {
      setFailed(true)
      setMessage('Not recorded. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div
      role="group"
      aria-labelledby={legendId}
      data-testid="mail-security-feedback"
      className="mt-2 rounded-lg border border-line px-3 py-2 text-xs"
    >
      <p id={legendId} className="font-semibold text-primary">
        Your review
      </p>
      <p className="mt-0.5 text-muted">
        This tells NerveShield what you think. It does not trust the sender,
        move mail or approve anything.
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {OPTIONS.map((o) => (
          <button
            key={o.verdict}
            type="button"
            aria-pressed={current === o.verdict}
            disabled={busy !== null}
            data-testid={`mail-security-feedback-${o.verdict}`}
            onClick={() => void submit(o.verdict)}
            className={`rounded border px-2 py-1 font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-60 ${
              current === o.verdict
                ? 'border-blue-500 bg-blue-50 text-blue-800 dark:bg-blue-900/30 dark:text-blue-200'
                : 'border-line text-secondary hover:bg-hover'
            }`}
          >
            {busy === o.verdict ? 'Saving…' : o.label}
          </button>
        ))}
      </div>
      <p
        role={failed ? 'alert' : 'status'}
        aria-live="polite"
        data-testid="mail-security-feedback-status"
        className={`mt-1.5 min-h-[1rem] ${failed ? 'text-red-700 dark:text-red-300' : 'text-muted'}`}
      >
        {message}
      </p>
    </div>
  )
}
