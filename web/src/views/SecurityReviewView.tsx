/**
 * SecurityReviewView — NerveShield Task 7.3 security review queue.
 *
 * A calm, prioritized list of flagged mail that nobody has reviewed yet
 * (R-020.5). The server orders it by impact and applies the attention
 * budget: only a few new, high-impact items are marked "new and high
 * impact", quiet hours hold them, and low-impact noise is counted in a
 * grouped digest instead of listed.
 *
 * SHADOW: nothing on this page notifies, moves mail or acts. Possible
 * commitments found in a flagged message are kept as review candidates and
 * shown by count only; they are not promoted from here.
 *
 * Inert text only: no remote resources, no HTML from the server.
 */

import { useCallback, useEffect, useId, useState } from 'react'
import { mailSecurityApi, type ReviewQueue, type ReviewQueueItem } from '../api/mailSecurity'
import { MailSecurityIndicator } from '../components/MailSecurityIndicator'

const TIER_LABEL: Record<ReviewQueueItem['tier'], string> = {
  CRITICAL: 'Critical',
  HIGH: 'High',
  MEDIUM: 'Medium',
  LOW: 'Low',
}

const DISPOSITION_LABEL: Record<string, string> = {
  RESTRICTED: 'Restricted',
  REVIEW: 'Review',
  INCOMPLETE: 'Incomplete',
  // Queued only by a SHADOW signal (thread continuity NS-ADR-067, campaign
  // NS-ADR-069); the reason labels say which.
  LOW_PRIORITY: 'Worth a look',
}

/** Plain-language labels for the server's queue reason codes. */
const REASON_LABEL: Record<string, string> = {
  PAYMENT_OR_CREDENTIAL_ASK: 'Asks for money or credentials',
  HIGH_SEVERITY_FINDING: 'High-severity finding',
  IMPORTANT_HUMAN_SENDER: 'From someone you marked important',
  COMMITMENTS_PRESERVED: 'Possible commitments kept for review',
  INTERRUPT_NOVEL_CRITICAL: 'New and high impact',
  INTERRUPT_DEFERRED_QUIET_PERIOD: 'Held for quiet hours',
  INTERRUPT_BUDGET_EXHAUSTED: "Over today's alert budget",
  NOT_NOVEL: 'Not new today',
  THREAD_BROKEN: "Doesn't fit the conversation it claims to continue",
  THREAD_SUSPECT: 'New participant in an existing conversation',
  CAMPAIGN_OF_FLAGGED: 'Looks like another message flagged for review',
}

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return 'Not recorded'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function commitmentSummary(item: ReviewQueueItem): string | null {
  if (item.commitments.length === 0) return null
  const counts: Record<string, number> = {}
  for (const c of item.commitments) counts[c.kind] = (counts[c.kind] ?? 0) + 1
  const parts = Object.entries(counts).map(([k, n]) => `${n} ${k}${n === 1 ? '' : 's'}`)
  return `Kept for review, not acted on: ${parts.join(', ')}`
}

function QueueRow({ item }: { item: ReviewQueueItem }) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const labels = item.reasons.map((r) => REASON_LABEL[r]).filter((l): l is string => !!l)
  const commitments = commitmentSummary(item)
  return (
    <li
      data-testid={`review-item-${item.assessment_id}`}
      data-tier={item.tier}
      data-delivery={item.delivery}
      className="rounded-lg border border-line bg-elevated px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded border border-current/40 px-1.5 py-0.5 text-[11px] font-semibold">
          {`${TIER_LABEL[item.tier]} impact`}
        </span>
        <span className="text-sm font-medium text-primary">
          {DISPOSITION_LABEL[item.disposition] ?? item.disposition}
        </span>
        {item.delivery === 'INTERRUPT' && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
            New and high impact
          </span>
        )}
        <span className="ml-auto text-[11px] text-muted">{formatWhen(item.created_at)}</span>
      </div>
      {labels.length > 0 && (
        <ul className="mt-1 flex flex-wrap gap-1 text-[11px] text-secondary">
          {labels.map((l) => (
            <li key={l} className="rounded bg-hover px-1.5 py-0.5">
              {l}
            </li>
          ))}
        </ul>
      )}
      {item.deliver_after && (
        <p className="mt-1 text-[11px] text-muted">
          {`Held until ${formatWhen(item.deliver_after)}`}
        </p>
      )}
      {commitments && (
        <p className="mt-1 text-[11px] text-secondary" data-testid="review-item-commitments">
          {commitments}
        </p>
      )}
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
        className="mt-2 rounded text-[11px] font-medium text-blue-600 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-400"
      >
        {open ? 'Hide security card' : 'Show security card'}
      </button>
      <div id={panelId} hidden={!open} className="mt-1.5">
        {open && <MailSecurityIndicator assessmentId={item.assessment_id} />}
      </div>
    </li>
  )
}

export function SecurityReviewView() {
  const [queue, setQueue] = useState<ReviewQueue | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(false)
    try {
      setQueue(await mailSecurityApi.getReviewQueue())
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const items = queue?.data ?? []
  const digest = queue?.digest ?? { count: 0, by_reason: {} }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-3xl font-bold text-primary">Security review</h1>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded border border-line px-3 py-1 text-sm text-secondary hover:bg-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
        >
          Refresh
        </button>
      </div>
      <p className="text-sm text-muted" data-testid="review-summary">
        {loading
          ? 'Loading…'
          : `${items.length} to review · ${digest.count} grouped below · Observation only: nothing here acts on your mail.`}
      </p>

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-900/30 dark:text-red-300"
        >
          The review queue could not be loaded.
        </div>
      )}

      {queue?.quiet_until && (
        <p
          data-testid="review-quiet"
          className="rounded-lg border border-line px-4 py-2 text-sm text-secondary"
        >
          {`Quiet hours until ${formatWhen(queue.quiet_until)}. Alerts are held; nothing is dropped.`}
        </p>
      )}

      {!loading && !error && items.length === 0 && digest.count === 0 && (
        <div
          data-testid="review-empty"
          className="rounded-lg border border-line p-4 text-sm text-muted"
        >
          Nothing needs review.
        </div>
      )}

      {items.length > 0 && (
        <ul className="space-y-2" data-testid="review-list" aria-label="Messages to review">
          {items.map((item) => (
            <QueueRow key={item.assessment_id} item={item} />
          ))}
        </ul>
      )}

      {digest.count > 0 && (
        <section data-testid="review-digest" className="rounded-lg border border-line px-4 py-3">
          <h2 className="text-sm font-semibold text-primary">
            {`${digest.count} lower-impact item${digest.count === 1 ? '' : 's'}, grouped`}
          </h2>
          <ul className="mt-1 space-y-0.5 text-xs text-secondary">
            {Object.entries(digest.by_reason).map(([code, n]) => (
              <li key={code}>
                <span className="font-mono">{code}</span>
                {` × ${n}`}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
