/**
 * Inline SVG icon set for NerveCenter web UI.
 *
 * Replaces the emoji glyph nav icons (audit defect D-4): one uniform set,
 * stroke-based (2px stroke on a 24-unit viewBox), rendered at 18px by
 * default (inside the required 16–20px band) and tinted with `currentColor`.
 * All icons are decorative (`aria-hidden="true"`); the accessible name of a
 * navigation item is always its text label.
 *
 * Geometry follows the common open-source stroke-icon conventions
 * (Lucide-style, ISC-licensed shapes) so the set reads as a single family.
 */
import type { SVGProps } from 'react'

export interface IconProps extends SVGProps<SVGSVGElement> {
  /** Rendered size in px; default 18 (uniform across the set). */
  size?: number
}

function baseProps(size: number, extra: IconProps): SVGProps<SVGSVGElement> {
  const { size: _size, ...rest } = extra
  return {
    viewBox: '0 0 24 24',
    width: size,
    height: size,
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': true,
    focusable: false,
    ...rest,
  }
}

/* ─── Navigation icons (Layout sidebar) ───────────────────────────────────── */

/** Overview — dashboard tiles. */

export function OverviewIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="3" y="3" width="7" height="9" rx="1" />
      <rect x="14" y="3" width="7" height="5" rx="1" />
      <rect x="14" y="12" width="7" height="9" rx="1" />
      <rect x="3" y="16" width="7" height="5" rx="1" />
    </svg>
  )
}

/** Calendar — availability grid (prompt2 P2). */
export function CalendarIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="3" y="4" width="18" height="17" rx="2" />
      <line x1="3" y1="9" x2="21" y2="9" />
      <line x1="3" y1="14" x2="21" y2="14" />
      <line x1="8" y1="2" x2="8" y2="5" />
      <line x1="16" y1="2" x2="16" y2="5" />
      <line x1="8" y1="14" x2="8" y2="21" />
      <line x1="16" y1="14" x2="16" y2="21" />
    </svg>
  )
}

/** Now — crosshair focus reticle. */
export function NowIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <circle cx="12" cy="12" r="8" />
      <line x1="12" y1="2" x2="12" y2="5" />
      <line x1="12" y1="19" x2="12" y2="22" />
      <line x1="2" y1="12" x2="5" y2="12" />
      <line x1="19" y1="12" x2="22" y2="12" />
      <circle cx="12" cy="12" r="1.5" />
    </svg>
  )
}

/** Briefing — a paper sheet with a horizontal divider (a one-screen daily brief). */
export function BriefingIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="4" y="3" width="16" height="18" rx="2" />
      <line x1="8" y1="7" x2="16" y2="7" />
      <line x1="8" y1="11" x2="16" y2="11" />
      <line x1="8" y1="15" x2="13" y2="15" />
    </svg>
  )
}

/** Messages — chat bubbles (a thread). */
export function MessagesIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M4 5h13a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H8l-4 3v-3H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" />
      <line x1="7" y1="9" x2="14" y2="9" />
    </svg>
  )
}

/** Email — an envelope. */
export function EmailIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <polyline points="3 7 12 13 21 7" />
    </svg>
  )
}

/** Contacts — a person silhouette (the §79 cross-channel person-resolution record). */
export function ContactsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 20a8 8 0 0 1 16 0" />
    </svg>
  )
}

/** Voicemail — a microphone (the §14/§15 ingestion source for the transcript). */
export function VoicemailIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10a7 7 0 0 0 14 0" />
      <line x1="12" y1="19" x2="12" y2="22" />
    </svg>
  )
}

/** Waiting On — a clock on an open loop (prompt2 §19 — an outgoing request
 * with no response yet: time is elapsing while the answer is still pending). */
export function WaitingOnIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M21 12a9 9 0 1 1-4.6-7.9" />
      <polyline points="21 4 21 12 13 12" />
      <path d="M12 12v4" />
    </svg>
  )
}

/** Urgent escalation — a warning triangle (the §22/§45 "urgent" policy on a
 * waiting-on request: follow up within 24h, or escalate to a VIP if
 * unresolved). Used by the Waiting-On plane's per-card indicator. */
export function AlertTriangleIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
      <path d="M12 9v4" />
      <path d="M12 17h.01" />
    </svg>
  )
}

/** Systems — stacked server nodes. */
export function SystemsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="2" y="3" width="20" height="7" rx="2" />
      <rect x="2" y="14" width="20" height="7" rx="2" />
      <line x1="6" y1="6.5" x2="6.01" y2="6.5" />
      <line x1="6" y1="17.5" x2="6.01" y2="17.5" />
    </svg>
  )
}

/** Timeline — clock face. */
export function TimelineIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <circle cx="12" cy="12" r="9" />
      <polyline points="12 7 12 12 15.5 14" />
    </svg>
  )
}

/** Goals — concentric target rings. */
export function GoalsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1" />
    </svg>
  )
}

/** Task DAG — checklist. */
export function TasksIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="m3 7 2 2 4-4" />
      <path d="m3 17 2 2 4-4" />
      <line x1="13" y1="6" x2="21" y2="6" />
      <line x1="13" y1="12" x2="21" y2="12" />
      <line x1="13" y1="18" x2="21" y2="18" />
    </svg>
  )
}

/** Agents — robot head. */
export function AgentsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="4" y="9" width="16" height="11" rx="2" />
      <line x1="12" y1="9" x2="12" y2="5" />
      <circle cx="12" cy="3.5" r="1.5" />
      <line x1="2" y1="14" x2="4" y2="14" />
      <line x1="20" y1="14" x2="22" y2="14" />
      <line x1="9" y1="14" x2="9.01" y2="16" />
      <line x1="15" y1="14" x2="15.01" y2="16" />
    </svg>
  )
}

/** Inference — CPU chip. */
export function InferenceIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <rect x="5" y="5" width="14" height="14" rx="2" />
      <rect x="9.5" y="9.5" width="5" height="5" />
      <line x1="9" y1="2" x2="9" y2="5" />
      <line x1="15" y1="2" x2="15" y2="5" />
      <line x1="9" y1="19" x2="9" y2="22" />
      <line x1="15" y1="19" x2="15" y2="22" />
      <line x1="2" y1="9" x2="5" y2="9" />
      <line x1="2" y1="15" x2="5" y2="15" />
      <line x1="19" y1="9" x2="22" y2="9" />
      <line x1="19" y1="15" x2="22" y2="15" />
    </svg>
  )
}

/** Incidents — warning triangle. */
export function IncidentsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M10.3 4.2 2.5 18a2 2 0 0 0 1.7 3h15.6a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="16.5" x2="12.01" y2="16.5" />
    </svg>
  )
}

/** Decisions — document with ruled text. */
export function DecisionsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="8" y1="13" x2="16" y2="13" />
      <line x1="8" y1="17" x2="13" y2="17" />
    </svg>
  )
}

/** Ask — speech bubble. */
export function AskIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.5 8.5 0 1 1 16.1-3.8Z" />
    </svg>
  )
}

/** Connections — linked nodes (two rings joined by a link). */
export function ConnectionsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <circle cx="7" cy="12" r="4" />
      <circle cx="17" cy="7" r="3" />
      <circle cx="17" cy="17" r="3" />
      <line x1="10.3" y1="10.4" x2="14.4" y2="8.2" />
      <line x1="10.3" y1="13.6" x2="14.4" y2="15.8" />
    </svg>
  )
}

/** Downloads — box with a downward arrow. */
export function DownloadsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M3 8h18l-1.5 11.3a2 2 0 0 1-2 1.7H6.5a2 2 0 0 1-2-1.7L3 8Z" />
      <path d="M3 8v-2a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v2" />
      <line x1="12" y1="11" x2="12" y2="16" />
      <path d="m9.5 13.5 2.5 2.5 2.5-2.5" />
    </svg>
  )
}

/* ─── Action icons ────────────────────────────────────────────────────────── */

/** Hamburger — opens the mobile navigation drawer. */
export function MenuIcon({ size = 20, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <line x1="4" y1="6" x2="20" y2="6" />
      <line x1="4" y1="12" x2="20" y2="12" />
      <line x1="4" y1="18" x2="20" y2="18" />
    </svg>
  )
}

/** Close — dismisses the mobile navigation drawer. */
export function CloseIcon({ size = 20, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

/** Shield check — SSO (federated identity) sign-in. */
export function ShieldCheckIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1 1 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  )
}

/** Settings — a vertical slider row (the settings panel). */
export function SettingsIcon({ size = 18, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, rest)}>
      <line x1="4" y1="8" x2="13" y2="8" />
      <line x1="17" y1="8" x2="20" y2="8" />
      <circle cx="15" cy="8" r="2" />
      <line x1="4" y1="16" x2="7" y2="16" />
      <line x1="11" y1="16" x2="20" y2="16" />
      <circle cx="9" cy="16" r="2" />
    </svg>
  )
}
