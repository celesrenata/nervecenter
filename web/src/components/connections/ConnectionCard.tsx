/**
 * Connection Center — one connection card.
 *
 * Surfaces every frozen capability against a single row:
 *   - two-axis badges (Auth + Operational, NEVER conflated) + state pill
 *   - enable/disable toggle (PATCH enabled)
 *   - verify, sync now (shows syncing state), reconnect, initial import
 *   - import progress (from `import_progress`)
 *   - last successful sync (relative + absolute; "never ingested" when absent)
 *   - diagnostics open, permissions open, remove (with confirmation)
 *
 * All actions call through the injected `actions` object so the card stays
 * testable (no fetch in the component itself).
 */

import { useState } from 'react'
import type { Connection } from '../../api/connections'
import { mapConnectionState, formatRelative } from './stateMapping'
import { StatePill, StatusBadgePair } from './badges'
import { ConnectionDiagnostics } from './ConnectionDiagnostics'
import { ConnectionPermissions } from './ConnectionPermissions'

/** Transient per-card feedback from the container (success / danger). */
export interface CardNotice {
  tone: 'success' | 'danger'
  text: string
}

export interface ConnectionCardActions {
  onToggleEnabled: (connection: Connection, enabled: boolean) => void
  onVerify: (connection: Connection) => void
  onSyncNow: (connection: Connection) => void
  onImport: (connection: Connection) => void
  onReconnect: (connection: Connection) => void
  onRemove: (connection: Connection) => void
  onShowDiagnostics: (connection: Connection) => void
  onShowPermissions: (connection: Connection) => void
}

const KIND_LABEL: Record<string, string> = {
  gmail: 'Gmail',
  imap: 'IMAP mailbox',
  graph_mail: 'Outlook / Microsoft 365',
  relay_device: 'Apple Messages relay',
  calendar: 'Calendar',
}

function displayName(connection: Connection): string {
  if (connection.label) return connection.label
  const account = connection.config?.account
  if (typeof account === 'string' && account.trim() !== '') return account
  return KIND_LABEL[connection.kind] ?? 'Connection'
}

/** Compact import-progress line from `import_progress` (job bookkeeping). */
function importProgressLine(connection: Connection): string | null {
  const progress = connection.import_progress
  if (!progress) return null
  const jobId = progress.last_job_id
  if (typeof jobId !== 'string' || jobId === '') return null
  const percent =
    typeof progress.percent === 'number' ? ` · ${Math.round(progress.percent)}%` : ''
  const count =
    typeof progress.messages === 'number' ? ` · ${progress.messages} messages` : ''
  return `Import job ${jobId.slice(0, 8)}…${percent}${count}`
}

export function ConnectionCard({
  connection,
  busy,
  actions,
  notice,
}: {
  connection: Connection
  /** True while an action on this card is in flight. */
  busy?: boolean
  actions: ConnectionCardActions
  /** Transient feedback from the container; cleared after the next action. */
  notice?: CardNotice | null
}) {
  const [panel, setPanel] = useState<'none' | 'diagnostics' | 'permissions'>('none')
  const [confirmingRemove, setConfirmingRemove] = useState(false)
  const presentation = mapConnectionState(connection)
  const lastSuccess = formatRelative(connection.last_success_at)
  const progressLine = importProgressLine(connection)

  return (
    <article className="bg-elevated rounded-lg shadow-lg border border-line p-5 space-y-4">
      {/* Header: name + state pill + enabled toggle */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-lg font-semibold text-primary break-words">
            {displayName(connection)}
          </h3>
          <p className="text-xs text-muted mt-0.5">
            {KIND_LABEL[connection.kind] ?? connection.kind}
            {typeof connection.config?.device_id === 'string' &&
              (connection.config.device_id as string) !== '' &&
              ` · ${(connection.config.device_id as string).slice(0, 12)}`}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatePill presentation={presentation} />
          <button
            type="button"
            role="switch"
            aria-checked={connection.enabled}
            aria-label={connection.enabled ? 'Disable connection' : 'Enable connection'}
            disabled={busy}
            onClick={() => actions.onToggleEnabled(connection, !connection.enabled)}
            className={`relative h-6 w-11 shrink-0 rounded-full border transition-colors disabled:opacity-50 ${
              connection.enabled ? 'bg-success border-success' : 'bg-hover border-line'
            }`}
          >
            <span
              aria-hidden="true"
              className={`absolute top-0.5 h-4.5 w-4.5 rounded-full bg-elevated shadow transition-all ${
                connection.enabled ? 'left-[22px]' : 'left-0.5'
              }`}
            />
            <span className="sr-only">{connection.enabled ? 'Enabled' : 'Disabled'}</span>
          </button>
        </div>
      </div>

      {/* The two SEPARATE status badges (hard rule) */}
      <StatusBadgePair presentation={presentation} />

      {/* Sync summary */}
      <dl className="text-sm text-secondary space-y-1">
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <div>
            <dt className="inline text-muted">Last successful sync: </dt>
            <dd className="inline">
              {lastSuccess
                ? `${lastSuccess.relative} (${lastSuccess.absolute})`
                : 'never ingested — login alone does not make this operational'}
            </dd>
          </div>
          {connection.last_attempt_at && (
            <div>
              <dt className="inline text-muted">Last attempted: </dt>
              <dd className="inline">{formatRelative(connection.last_attempt_at)?.relative}</dd>
            </div>
          )}
        </div>
        {progressLine && <dd className="text-xs text-muted">{progressLine}</dd>}
        {connection.last_error_code && (
          <dd className="text-xs text-danger break-words">
            Last error: <code>{connection.last_error_code}</code>
          </dd>
        )}
      </dl>

      {notice && (
        <p
          role={notice.tone === 'danger' ? 'alert' : 'status'}
          className={`rounded-md border px-3 py-2 text-sm ${
            notice.tone === 'danger'
              ? 'border-[color-mix(in_srgb,var(--danger)_40%,transparent)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] text-danger'
              : 'border-[color-mix(in_srgb,var(--success)_35%,transparent)] bg-[color-mix(in_srgb,var(--success)_12%,transparent)] text-success'
          }`}
        >
          {notice.text}
        </p>
      )}

      {/* Actions */}
      {confirmingRemove ? (
        <div
          className="rounded-md border border-[color-mix(in_srgb,var(--danger)_40%,transparent)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] p-3 text-sm"
          role="alertdialog"
          aria-label="Confirm removing this connection"
        >
          <p className="text-primary font-medium">
            Remove this connection?
          </p>
          <p className="text-secondary mt-1">
            The account credentials and stored secrets are purged from the vault.
            Any history already ingested into NerveCenter stays in place.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              autoFocus
              onClick={() => actions.onRemove(connection)}
              disabled={busy}
              className="rounded-md bg-danger px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              Remove & purge secrets
            </button>
            <button
              type="button"
              onClick={() => setConfirmingRemove(false)}
              className="rounded-md border border-line px-3 py-1.5 text-sm text-secondary hover:bg-hover"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <ActionButton label="Verify" busy={busy} onClick={() => actions.onVerify(connection)} />
          <ActionButton
            label="Sync now"
            busy={busy || connection.operational_status === 'syncing'}
            onClick={() => actions.onSyncNow(connection)}
          />
          <ActionButton label="Import history" busy={busy} onClick={() => actions.onImport(connection)} />
          <ActionButton label="Reconnect" busy={busy} onClick={() => actions.onReconnect(connection)} />
          <ActionButton
            label="Diagnostics"
            onClick={() => setPanel(panel === 'diagnostics' ? 'none' : 'diagnostics')}
          />
          <ActionButton
            label="Permissions"
            onClick={() => setPanel(panel === 'permissions' ? 'none' : 'permissions')}
          />
          <button
            type="button"
            onClick={() => setConfirmingRemove(true)}
            disabled={busy}
            className="ml-auto rounded-md border border-[color-mix(in_srgb,var(--danger)_45%,transparent)] px-3 py-1.5 text-sm text-danger hover:bg-[color-mix(in_srgb,var(--danger)_10%,transparent)] disabled:opacity-50"
          >
            Remove
          </button>
        </div>
      )}

      {/* Expandable panels */}
      {panel === 'diagnostics' && (
        <ConnectionDiagnostics
          connectionId={connection.id}
          refreshKey={connection.updated_at}
        />
      )}
      {panel === 'permissions' && (
        <ConnectionPermissions
          connection={connection}
          onChanged={(updated) => actions.onShowPermissions(updated)}
        />
      )}
    </article>
  )
}

function ActionButton({
  label,
  onClick,
  busy,
}: {
  label: string
  onClick: () => void
  busy?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className="rounded-md border border-line px-3 py-1.5 text-sm text-secondary hover:bg-hover hover:text-primary transition-colors disabled:opacity-50"
    >
      {label}
    </button>
  )
}
