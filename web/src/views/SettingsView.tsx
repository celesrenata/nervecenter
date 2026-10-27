import type { ReactNode } from 'react'
import { useTheme, type Theme } from '../theme/ThemeContext'
import {
  useSettings,
  ACCENT_SWATCH,
  type AccentId,
  type FontScaleId,
  type ReducedMotion,
} from '../theme/SettingsContext'
import { Link } from 'react-router-dom'

/**
 * Settings — the service settings panel.
 *
 * Three sections, matching the requested scope:
 *   1. Appearance & global attributes — the user-visible theme controls
 *      (dark / light / system via :mod:`ThemeContext`) plus the global
 *      attributes owned by :mod:`SettingsContext` (accent color, font scale,
 *      reduce-motion). Every control persists to localStorage and is applied
 *      to the app in an effect, so the whole app re-styles live.
 *   2. Connectors — how connectors work and how to use/verify them (the
 *      mail-connector registry + the macOS Personal Relay that owns the
 *      source-side capture). Instructional; links to the live endpoints.
 *   3. Apps — how to download the self-hosted build and sideload it onto a
 *      paired device, with a link to the live Downloads manifest.
 *
 * The panel renders the same way in mock mode (no `VITE_API_BASE`) and real
 * mode — the appearance controls are local-only and the two instruction
 * sections are static, so there is nothing to fail against the network.
 */

const THEME_OPTIONS: Array<{ id: Theme; label: string; hint: string }> = [
  { id: 'system', label: 'System', hint: 'Follow the OS color scheme' },
  { id: 'light', label: 'Light', hint: 'Always the light palette' },
  { id: 'dark', label: 'Dark', hint: 'Always the dark palette' },
]

const ACCENT_OPTIONS: Array<{ id: AccentId; label: string }> = [
  { id: 'blue', label: 'Blue' },
  { id: 'emerald', label: 'Emerald' },
  { id: 'violet', label: 'Violet' },
  { id: 'amber', label: 'Amber' },
  { id: 'rose', label: 'Rose' },
]

const FONT_SCALE_OPTIONS: Array<{ id: FontScaleId; label: string }> = [
  { id: 'sm', label: 'Small' },
  { id: 'md', label: 'Medium' },
  { id: 'lg', label: 'Large' },
]

const MOTION_OPTIONS: Array<{ id: ReducedMotion; label: string; hint: string }> = [
  { id: 'system', label: 'Match system', hint: 'Follow the OS Reduce Motion setting' },
  { id: 'reduce', label: 'Reduce', hint: 'Minimize animation and transitions' },
  { id: 'full', label: 'Full', hint: 'Always show full motion' },
]

export function SettingsView() {
  const { theme, setTheme } = useTheme()
  const { settings, update, reset } = useSettings()

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-3xl font-bold text-primary">Settings</h1>
        <button
          type="button"
          onClick={reset}
          className="text-sm text-secondary hover:text-primary border border-line rounded-lg px-3 py-1.5 hover:bg-hover transition-colors"
        >
          Reset to defaults
        </button>
      </div>

      <p className="text-sm text-muted max-w-2xl">
        These preferences are stored locally in this browser and applied to the
        whole app. They are yours alone — NerveCenter does not sync them to the
        server.
      </p>

      {/* ── Section 1 — Appearance & global attributes ───────────────────── */}
      <section className="bg-elevated rounded-lg shadow-lg p-6 space-y-6" aria-labelledby="settings-appearance">
        <div>
          <h2 id="settings-appearance" className="text-xl font-semibold text-primary">
            Appearance
          </h2>
          <p className="text-sm text-muted mt-1">
            Choose the theme, accent color, text size, and how much motion the UI uses.
          </p>
        </div>

        <OptionGroup label="Theme" hint="Which palette the app uses.">
          <Segmented
            options={THEME_OPTIONS.map((o) => ({
              id: o.id,
              label: o.label,
              hint: o.hint,
            }))}
            value={theme}
            onSelect={(id) => setTheme(id as Theme)}
          />
        </OptionGroup>

        <OptionGroup label="Accent color" hint="Used for buttons, links, and active highlights.">
          <div className="flex flex-wrap gap-3" role="radiogroup" aria-label="Accent color">
            {ACCENT_OPTIONS.map((o) => {
              const selected = settings.accent === o.id
              return (
                <button
                  key={o.id}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => update({ accent: o.id })}
                  className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
                    selected
                      ? 'border-accent bg-accent-soft text-accent-contrast font-medium'
                      : 'border-line text-secondary hover:bg-hover'
                  }`}
                >
                  <span
                    aria-hidden="true"
                    className="w-4 h-4 rounded-full shrink-0"
                    style={{ backgroundColor: ACCENT_SWATCH[o.id] }}
                  />
                  <span>{o.label}</span>
                </button>
              )
            })}
          </div>
        </OptionGroup>

        <OptionGroup label="Text size" hint="Scales the whole interface.">
          <Segmented
            options={FONT_SCALE_OPTIONS.map((o) => ({ id: o.id, label: o.label }))}
            value={settings.fontScale}
            onSelect={(id) => update({ fontScale: id as FontScaleId })}
          />
        </OptionGroup>

        <OptionGroup label="Motion" hint="How much animation the app plays.">
          <Segmented
            options={MOTION_OPTIONS.map((o) => ({ id: o.id, label: o.label, hint: o.hint }))}
            value={settings.reducedMotion}
            onSelect={(id) => update({ reducedMotion: id as ReducedMotion })}
          />
        </OptionGroup>
      </section>

      {/* ── Section 2 — Connector usage instructions ─────────────────────── */}
      <section className="bg-elevated rounded-lg shadow-lg p-6 space-y-4" aria-labelledby="settings-connectors">
        <div>
          <h2 id="settings-connectors" className="text-xl font-semibold text-primary">
            Connectors
          </h2>
          <p className="text-sm text-muted mt-1">
            How source connectors work and how to register and verify one.
          </p>
        </div>

        <div className="space-y-4 text-sm text-secondary leading-relaxed">
          <p>
            NerveCenter never touches a mailbox itself. The <strong>macOS Personal
            Relay</strong> ({'<code>nerve-personal-relay</code>'} on a trusted Mac) owns the
            source-side capture — it reads your mail with the permissions you grant it and
            posts normalized events to the API. What the backend keeps is a{' '}
            <strong>connector registry</strong>: <em>where</em> the relay reads from, which
            auth method is in use, and whether that mailbox currently answers.
          </p>

          <div className="rounded-md border border-line bg-hover/40 p-4">
            <p className="font-medium text-primary mb-2">Registering & verifying a connector</p>
            <ol className="list-decimal list-inside space-y-1.5">
              <li>
                Register the mailbox with{' '}
                <code className="text-xs">POST /api/mail-connectors</code>. The payload names
                the account, host, port, and auth method — <strong>never the password</strong>{' '}
                (only an opaque <code className="text-xs">token_ref</code> naming where the
                credential is kept).
              </li>
              <li>
                Run a liveness probe with{' '}
                <code className="text-xs">POST /api/mail-connectors/&#123;id&#125;/verify</code>,
                supplying the credential for that call only. It is used to probe once and
                then discarded — the result (CONNECTED / AUTH_EXPIRED / ERROR) is what is
                stored.
              </li>
              <li>
                Manage the registry with{' '}
                <code className="text-xs">PATCH</code> / <code className="text-xs">DELETE</code>{' '}
                <code className="text-xs">/api/mail-connectors/&#123;id&#125;</code> and toggle
                it with <code className="text-xs">POST …/disable</code> and{' '}
                <code className="text-xs">POST …/enable</code>.
              </li>
            </ol>
          </div>

          <p>
            A connector is a description plus a live probe, not a held socket: a changed
            host or port resets its status to <strong>PENDING</strong> until you re-verify.
            An unconfigured system reports an empty registry — never a fabricated connector.
          </p>

          <p className="text-muted">
            Full endpoint shapes are documented in{' '}
            <code className="text-xs">web/docs/API_REFERENCE.md</code> and the{' '}
            <code className="text-xs">apple/Distribution</code> notes for the relay build.
          </p>
        </div>

        <div className="rounded-md border border-line bg-hover/40 p-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-medium text-primary mb-1">Manage your connections</p>
            <p className="text-sm text-secondary">
              Register, verify, sync, and diagnose every connector — and see each
              connection's live status at a glance.
            </p>
          </div>
          <Link
            to="/connections"
            className="px-4 py-2 rounded-lg bg-accent text-accent-contrast text-sm font-medium hover:opacity-90"
          >
            Open Connections
          </Link>
        </div>
      </section>

      {/* ── Section 3 — App download & sideload instructions ─────────────── */}
      <section className="bg-elevated rounded-lg shadow-lg p-6 space-y-4" aria-labelledby="settings-download">
        <div>
          <h2 id="settings-download" className="text-xl font-semibold text-primary">
            Apps — download & sideload
          </h2>
          <p className="text-sm text-muted mt-1">
            Get the self-hosted iOS / watchOS builds and install them on a paired device.
          </p>
        </div>

        <div className="space-y-4 text-sm text-secondary leading-relaxed">
          <p>
            The app ships as a self-hosted build you publish yourself — no App Store. The{' '}
            <Link to="/downloads" className="text-accent hover:underline font-medium">
              Downloads
            </Link>{' '}
            screen lists the current artifacts (the iPhone <code className="text-xs">.ipa</code>,
            the watchOS <code className="text-xs">.app</code>, and the watch widget archive)
            with their versions, sizes, and SHA-256 checksums, read from a static{' '}
            <code className="text-xs">manifest.json</code> under <code className="text-xs">/downloads</code>.
          </p>

          <div className="rounded-md border border-line bg-hover/40 p-4">
            <p className="font-medium text-primary mb-2">Sideload the iPhone build</p>
            <ol className="list-decimal list-inside space-y-1.5">
              <li>
                Build the artifacts on the macOS build host with{' '}
                <code className="text-xs">export_app.sh</code> (or{' '}
                <code className="text-xs">fastlane</code>), which signs with a personal-team
                ad-hoc profile and publishes them under <code className="text-xs">/downloads</code>.
              </li>
              <li>
                Download <code className="text-xs">NervePhone.ipa</code> from the Downloads
                screen.
              </li>
              <li>
                Install it onto a paired, developer-mode device via Apple Configurator, a
                device-management (MDM) profile, or the{' '}
                <code className="text-xs">devicectl</code> /{' '}
                <code className="text-xs">cfg</code> tooling.
              </li>
              <li>
                Trust the developer profile on the device (Settings → General → VPN &
                Device Management), then launch the app.
              </li>
            </ol>
          </div>

          <p>
            The watchOS app (<code className="text-xs">NerveWatch.app</code>) is installed
            onto a paired Apple Watch together with the watchOS widget archive; the iOS
            widget ships embedded in <code className="text-xs">NervePhone.ipa</code> and is
            marked as such on the Downloads screen. Ad-hoc builds are signed with a
            personal-team profile, so an install can lapse when the profile expires — re-run
            the export and reinstall to refresh it.
          </p>

          <p className="text-muted">
            See <code className="text-xs">apple/Distribution/README.md</code> for the full
            signing and publishing walkthrough.
          </p>
        </div>
      </section>
    </div>
  )
}

// ─── Small presentational helpers (view-local) ─────────────────────────────────

function OptionGroup({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <div className="space-y-2">
      <div>
        <p className="text-sm font-medium text-primary">{label}</p>
        {hint && <p className="text-xs text-muted mt-0.5">{hint}</p>}
      </div>
      {children}
    </div>
  )
}

interface SegmentedOption {
  id: string
  label: string
  hint?: string
}

function Segmented({
  options,
  value,
  onSelect,
}: {
  options: SegmentedOption[]
  value: string
  onSelect: (id: string) => void
}) {
  return (
    <div className="inline-flex rounded-lg border border-line overflow-hidden" role="radiogroup">
      {options.map((o, i) => {
        const selected = value === o.id
        return (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={selected}
            title={o.hint}
            onClick={() => onSelect(o.id)}
            className={`px-4 py-2 text-sm transition-colors ${
              i > 0 ? 'border-l border-line' : ''
            } ${
              selected
                ? 'bg-accent-soft text-accent-contrast font-medium'
                : 'text-secondary hover:bg-hover'
            }`}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}
