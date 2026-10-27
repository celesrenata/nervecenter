/**
 * Connection Center — status badges (spec §3 hard rule).
 *
 * Authentication status and operational status are ALWAYS rendered as two
 * SEPARATE badges, each with its own label prefix. A connection that passed
 * a login probe but never ingested data shows Auth = "Authenticated" next
 * to Operational = "Never synced" — it must never read as operational or
 * "healthy" until a real ingest lands (`last_success_at` / healthy).
 */

import type { StatePresentation } from './stateMapping'

export type Tone = 'neutral' | 'success' | 'warning' | 'danger'

/**
 * Tailwind arbitrary-value tints: the semantic colors are CSS variables
 * (theme-driven), so opacity is mixed with `color-mix` instead of Tailwind
 * opacity modifiers (which do not resolve against `var()` colors in this
 * project).
 */
const BADGE_TONE: Record<Tone, string> = {
  neutral: 'bg-hover text-secondary border-line',
  success:
    'text-success border-[color-mix(in_srgb,var(--success)_35%,transparent)] bg-[color-mix(in_srgb,var(--success)_12%,transparent)]',
  warning:
    'text-warning border-[color-mix(in_srgb,var(--warning)_35%,transparent)] bg-[color-mix(in_srgb,var(--warning)_12%,transparent)]',
  danger:
    'text-danger border-[color-mix(in_srgb,var(--danger)_35%,transparent)] bg-[color-mix(in_srgb,var(--danger)_12%,transparent)]',
}

export function toneBadgeClass(tone: Tone): string {
  return `inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs leading-5 ${BADGE_TONE[tone]}`
}

/**
 * One labelled status badge. `axis` is the small prefix ("Auth" or
 * "Operational") that keeps the two axes unambiguous.
 */
export function StatusBadge({
  axis,
  label,
  tone,
}: {
  axis: string
  label: string
  tone: Tone
}) {
  return (
    <span className={toneBadgeClass(tone)} role="status" aria-label={`${axis}: ${label}`}>
      <span className="text-[10px] font-semibold uppercase tracking-wide opacity-70">{axis}</span>
      <span className="font-medium">{label}</span>
    </span>
  )
}

/**
 * The derived five-state pill (Connected / Syncing / Degraded /
 * Authentication Required / Not Configured). It is a summary — the two
 * badges below it remain the source of truth for each axis.
 */
export function StatePill({ presentation }: { presentation: StatePresentation }) {
  return (
    <span
      className={toneBadgeClass(presentation.stateTone)}
      aria-label={`State: ${presentation.label}`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          presentation.stateTone === 'success'
            ? 'bg-success'
            : presentation.stateTone === 'warning'
              ? 'bg-warning'
              : presentation.stateTone === 'danger'
                ? 'bg-danger'
                : 'bg-muted'
        }`}
        aria-hidden="true"
      />
      <span className="font-semibold">{presentation.label}</span>
    </span>
  )
}

/**
 * The two-axis badge pair — rendered together on every card so auth and
 * operational status can never be conflated.
 */
export function StatusBadgePair({ presentation }: { presentation: StatePresentation }) {
  return (
    <div className="flex flex-wrap gap-2" aria-label="Authentication and operational status">
      <StatusBadge axis="Auth" label={presentation.auth.label} tone={presentation.auth.tone} />
      <StatusBadge
        axis="Operational"
        label={presentation.operational.label}
        tone={presentation.operational.tone}
      />
    </div>
  )
}
