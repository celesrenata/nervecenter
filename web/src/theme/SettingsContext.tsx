import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useTheme, type EffectiveTheme } from './ThemeContext'

/**
 * Global attribute settings for the NerveCenter web app (Settings panel).
 *
 * Sibling of :mod:`ThemeContext`: the theme controls (dark / light / system)
 * live there; this provider owns the *other* user-visible attributes —
 * accent color, font scale, and the reduce-motion preference.
 *
 * All preferences persist to localStorage (best-effort, like the theme) and
 * are applied synchronously to `<html>` so they are live before React paints
 * (no flash on reload). The accent override is applied by setting the
 * `--accent` / `--accent-hover` / `--accent-soft` / `--accent-contrast` /
 * `--ring` CSS variables on the root element — the semantic tokens
 * `tailwind.config.js` already maps, so every accent-styled control in the
 * app follows the selection with no per-view changes. `blue` (the default)
 * clears the overrides and defers to the theme's own accent.
 */

export type AccentId = 'blue' | 'emerald' | 'violet' | 'amber' | 'rose'
export type FontScaleId = 'sm' | 'md' | 'lg'
export type ReducedMotion = 'system' | 'reduce' | 'full'
export type BriefingKindSetting = 'morning' | 'midday' | 'evening'
export type WellnessTrendDays = 7 | 30 | 90
export type IncidentSeverity = 'low' | 'medium' | 'high' | 'critical'
export type LandingPage = 'today' | 'overview' | 'now'

export interface Settings {
  accent: AccentId
  fontScale: FontScaleId
  reducedMotion: ReducedMotion
  defaultBriefingKind: BriefingKindSetting
  wellnessTrendDays: WellnessTrendDays
  defaultIncidentSeverity: IncidentSeverity
  landingPage: LandingPage
  /**
   * Ask default model (browser-local). Empty string = no per-request model
   * override (the service default applies). Set from the Settings → Inference
   * tab; the Ask advanced picker initializes from it and syncs changes back.
   */
  askModel: string
  /** Ask default provider (browser-local). Empty = no override. */
  askProvider: string
}

export const DEFAULT_SETTINGS: Settings = {
  accent: 'blue',
  fontScale: 'md',
  reducedMotion: 'system',
  defaultBriefingKind: 'morning',
  wellnessTrendDays: 30,
  defaultIncidentSeverity: 'high',
  landingPage: 'today',
  askModel: '',
  askProvider: '',
}

const STORAGE_KEY = 'nerve-settings'

/**
 * Accent palettes per resolved theme. `blue` is the default: selecting it
 * removes the overrides entirely so the theme's own accent (index.css) wins.
 * The values mirror the token set in index.css so each accent is tuned for
 * both the dark and light palettes.
 */
export interface AccentTokenSet {
  accent: string
  accentHover: string
  accentSoft: string
  accentContrast: string
  ring: string
}

export const ACCENT_PRESETS: Record<
  Exclude<AccentId, 'blue'>,
  { light: AccentTokenSet; dark: AccentTokenSet }
> = {
  emerald: {
    light: {
      accent: '#059669',
      accentHover: '#047857',
      accentSoft: 'rgba(5, 150, 105, 0.08)',
      accentContrast: '#047857',
      ring: '#059669',
    },
    dark: {
      accent: '#10b981',
      accentHover: '#34d399',
      accentSoft: 'rgba(16, 185, 129, 0.25)',
      accentContrast: '#a7f3d0',
      ring: '#34d399',
    },
  },
  violet: {
    light: {
      accent: '#7c3aed',
      accentHover: '#6d28d9',
      accentSoft: 'rgba(124, 58, 237, 0.08)',
      accentContrast: '#6d28d9',
      ring: '#7c3aed',
    },
    dark: {
      accent: '#8b5cf6',
      accentHover: '#a78bfa',
      accentSoft: 'rgba(139, 92, 246, 0.25)',
      accentContrast: '#ddd6fe',
      ring: '#a78bfa',
    },
  },
  amber: {
    light: {
      accent: '#d97706',
      accentHover: '#b45309',
      accentSoft: 'rgba(217, 119, 6, 0.08)',
      accentContrast: '#b45309',
      ring: '#d97706',
    },
    dark: {
      accent: '#f59e0b',
      accentHover: '#fbbf24',
      accentSoft: 'rgba(245, 158, 11, 0.25)',
      accentContrast: '#fde68a',
      ring: '#fbbf24',
    },
  },
  rose: {
    light: {
      accent: '#e11d48',
      accentHover: '#be123c',
      accentSoft: 'rgba(225, 29, 72, 0.08)',
      accentContrast: '#be123c',
      ring: '#e11d48',
    },
    dark: {
      accent: '#f43f5e',
      accentHover: '#fb7185',
      accentSoft: 'rgba(244, 63, 94, 0.25)',
      accentContrast: '#fecdd3',
      ring: '#fb7185',
    },
  },
}

/** Preview swatch (mid-tone) for the settings UI. */
export const ACCENT_SWATCH: Record<AccentId, string> = {
  blue: '#3b82f6',
  emerald: '#10b981',
  violet: '#8b5cf6',
  amber: '#f59e0b',
  rose: '#f43f5e',
}

/** Root font-size (px) per scale, on the default 16px root. */
export const FONT_SCALE_PX: Record<FontScaleId, number> = {
  sm: 14.4, // 0.9 ×
  md: 16, // 1.0 ×
  lg: 17.6, // 1.1 ×
}

export interface SettingsContextValue {
  settings: Settings
  /** Update one or more attributes and persist. */
  update: (patch: Partial<Settings>) => void
  /** Restore the defaults and persist. */
  reset: () => void
}

const SettingsContext = createContext<SettingsContextValue | null>(null)

function isAccentId(v: unknown): v is AccentId {
  return v === 'blue' || v === 'emerald' || v === 'violet' || v === 'amber' || v === 'rose'
}
function isFontScaleId(v: unknown): v is FontScaleId {
  return v === 'sm' || v === 'md' || v === 'lg'
}
function isReducedMotion(v: unknown): v is ReducedMotion {
  return v === 'system' || v === 'reduce' || v === 'full'
}

/**
 * Read the stored settings (no side effects), tolerating a missing or
 * corrupted localStorage entry. Used to seed the provider and to apply the
 * attributes before first paint.
 */
export function readStoredSettings(): Settings {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Settings> | null
      if (parsed && typeof parsed === 'object') {
        return {
          accent: isAccentId(parsed.accent) ? parsed.accent : DEFAULT_SETTINGS.accent,
          fontScale: isFontScaleId(parsed.fontScale) ? parsed.fontScale : DEFAULT_SETTINGS.fontScale,
          reducedMotion: isReducedMotion(parsed.reducedMotion)
            ? parsed.reducedMotion
            : DEFAULT_SETTINGS.reducedMotion,
          defaultBriefingKind: parsed.defaultBriefingKind === 'midday' || parsed.defaultBriefingKind === 'evening'
            ? parsed.defaultBriefingKind
            : DEFAULT_SETTINGS.defaultBriefingKind,
          wellnessTrendDays: parsed.wellnessTrendDays === 7 || parsed.wellnessTrendDays === 90
            ? parsed.wellnessTrendDays
            : DEFAULT_SETTINGS.wellnessTrendDays,
          defaultIncidentSeverity: parsed.defaultIncidentSeverity === 'low' || parsed.defaultIncidentSeverity === 'medium' || parsed.defaultIncidentSeverity === 'critical'
            ? parsed.defaultIncidentSeverity
            : DEFAULT_SETTINGS.defaultIncidentSeverity,
          landingPage: parsed.landingPage === 'overview' || parsed.landingPage === 'now'
            ? parsed.landingPage
            : DEFAULT_SETTINGS.landingPage,
          // Ask default model / provider (browser-local; empty = service default).
          askModel:
            typeof parsed.askModel === 'string' ? parsed.askModel : DEFAULT_SETTINGS.askModel,
          askProvider:
            typeof parsed.askProvider === 'string'
              ? parsed.askProvider
              : DEFAULT_SETTINGS.askProvider,
        }
      }
    }
  } catch {
    // localStorage unavailable (private mode) or corrupt — fall back to defaults.
  }
  return { ...DEFAULT_SETTINGS }
}

/**
 * Apply the global attributes to `<html>` synchronously.
 *
 * Call from main.tsx before `createRoot(...).render(...)` so the stored
 * settings are live before React paints (no flash); the SettingsProvider's
 * effect re-applies the same state afterwards and keeps it in sync.
 * `effectiveTheme` selects which accent palette applies (the provider keeps
 * it in sync with the live theme, including 'system' resolving to light/dark).
 */
export function applySettingsToDocument(settings: Settings, effectiveTheme: EffectiveTheme): void {
  const root = document.documentElement

  // Font scale — the whole UI scales through the root font-size.
  root.style.fontSize = `${FONT_SCALE_PX[settings.fontScale]}px`

  // Reduce motion — the CSS rule in index.css keys off this attribute
  // (mirroring the OS-level @media rule). 'system' defers to the OS
  // preference by clearing the attribute.
  if (settings.reducedMotion === 'reduce') {
    root.setAttribute('data-reduced-motion', 'reduce')
  } else {
    root.removeAttribute('data-reduced-motion')
  }

  // Accent overrides — blue defers to the theme's own accent.
  const tokens = settings.accent === 'blue' ? null : ACCENT_PRESETS[settings.accent][effectiveTheme]
  if (tokens) {
    root.style.setProperty('--accent', tokens.accent)
    root.style.setProperty('--accent-hover', tokens.accentHover)
    root.style.setProperty('--accent-soft', tokens.accentSoft)
    root.style.setProperty('--accent-contrast', tokens.accentContrast)
    root.style.setProperty('--ring', tokens.ring)
  } else {
    root.style.removeProperty('--accent')
    root.style.removeProperty('--accent-hover')
    root.style.removeProperty('--accent-soft')
    root.style.removeProperty('--accent-contrast')
    root.style.removeProperty('--ring')
  }
}

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<Settings>(readStoredSettings)

  // The effective theme (what the UI actually renders) picks the accent
  // palette. Re-applied whenever the theme or the accent changes so an
  // accent survives a dark ↔ light flip.
  const { effectiveTheme } = useTheme()

  useEffect(() => {
    applySettingsToDocument(settings, effectiveTheme)
  }, [settings, effectiveTheme])

  const update = useCallback((patch: Partial<Settings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch }
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
      } catch {
        // Persistence is best-effort; the in-memory preference still applies.
      }
      return next
    })
  }, [])

  const reset = useCallback(() => {
    setSettings({ ...DEFAULT_SETTINGS })
    try {
      window.localStorage.removeItem(STORAGE_KEY)
    } catch {
      // best-effort
    }
  }, [])

  const value = useMemo<SettingsContextValue>(
    () => ({ settings, update, reset }),
    [settings, update, reset],
  )

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>
}

/**
 * Access the global attribute settings.
 */
export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext)
  if (!ctx) throw new Error('useSettings must be used within a SettingsProvider')
  return ctx
}
