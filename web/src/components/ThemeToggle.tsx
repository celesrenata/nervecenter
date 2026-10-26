import { useTheme, type Theme } from '../theme/ThemeContext'

function MoonIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  )
}

function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  )
}

/** A filled-circle-with-halo mark that reads as "follow the system". */
function AutoIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1" />
      <circle cx="12" cy="12" r="3.5" />
    </svg>
  )
}

/**
 * Preference cycle for the header toggle. The order matters: it must include
 * `system` so a user who toggled once can always return to following the OS —
 * the single binary light/dark flip was the A-1 gap (visual-lift-audit §1.1).
 */
const CYCLE: Theme[] = ['dark', 'light', 'system']

const LABEL: Record<Theme, string> = {
  dark: 'Dark',
  light: 'Light',
  system: 'System',
}

export function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const idx = CYCLE.indexOf(theme)
  const next = CYCLE[(idx + 1) % CYCLE.length]

  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-[var(--border)] px-3 text-sm text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
      aria-label={`Theme: ${LABEL[theme]}. Activate to switch to ${LABEL[next]}.`}
      title={`Theme: ${LABEL[theme]} (next: ${LABEL[next]})`}
    >
      {theme === 'dark' ? <MoonIcon /> : theme === 'light' ? <SunIcon /> : <AutoIcon />}
      <span>{LABEL[theme]}</span>
    </button>
  )
}
