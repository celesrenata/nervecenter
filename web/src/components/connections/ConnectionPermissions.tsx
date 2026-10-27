/**
 * Connection Center — permission / config editor.
 *
 * Shows the effective permissions surface for the connection's kind
 * (`GET /permissions` → `editable_keys` + `denied_keys`) and lets the user
 * edit ONLY the allow-listed config fields. The backend silently ignores
 * every non-allow-listed key (especially anything credential-like) and
 * reports `applied_keys` / `ignored_keys` — we surface both.
 */

import { useEffect, useState } from 'react'
import {
  connectionsApi,
  type Connection,
  type ConnectionPermissions,
  isDegradedWrite,
} from '../../api/connections'

const FIELD_HINTS: Record<string, string> = {
  account: 'The mailbox or account address',
  host: 'The server host name',
  port: 'The TCP port (IMAPS standard: 993)',
  ssl: 'Use TLS/SSL',
  tenant_id: 'Azure / M365 tenant ID (optional)',
  oauth_redirect_uri: 'Where the provider returns you after sign-in',
  device_name: 'Human-readable device name',
}

export function ConnectionPermissions({
  connection,
  onChanged,
}: {
  connection: Connection
  /** Called with the refreshed row after a successful patch. */
  onChanged: (updated: Connection) => void
}) {
  const [permissions, setPermissions] = useState<ConnectionPermissions | null>(null)
  const [fields, setFields] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ applied: string[]; ignored: string[] } | null>(null)

  useEffect(() => {
    let cancelled = false
    setPermissions(null)
    connectionsApi
      .getPermissions(connection.id)
      .then((perms) => {
        if (cancelled) return
        setPermissions(perms)
        const editable = new Set(perms.editable_keys)
        const current: Record<string, string> = {}
        for (const [key, value] of Object.entries(connection.config ?? {})) {
          if (editable.has(key)) {
            current[key] =
              typeof value === 'string' ? value : value === null || value === undefined ? '' : String(value)
          }
        }
        setFields(current)
      })
      .catch((err: unknown) => setError(errMessage(err)))
    return () => {
      cancelled = true
    }
    // Re-seed only when the row's config actually changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connection.id, connection.config, connection.updated_at])

  const editableKeys = permissions?.editable_keys ?? []

  async function save() {
    if (saving || !permissions) return
    setSaving(true)
    setError(null)
    setResult(null)
    try {
      const payload: Record<string, unknown> = {}
      for (const key of permissions.editable_keys) {
        const value = fields[key] ?? ''
        if (key === 'port') {
          payload[key] = value === '' ? null : Number(value)
        } else if (key === 'ssl') {
          payload[key] = value === 'true'
        } else {
          payload[key] = value
        }
      }
      const res = await connectionsApi.updatePermissions(connection.id, payload)
      if (isDegradedWrite(res)) {
        setError(res.detail)
      } else {
        setResult({ applied: res.applied_keys, ignored: res.ignored_keys })
        onChanged(res)
      }
    } catch (err) {
      setError(errMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="rounded-md border border-line bg-hover/40 p-4 space-y-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Permissions & editable settings
        </h4>
        <span className="text-xs text-muted">
          {permissions ? `${permissions.editable_keys.length} editable field(s)` : 'Loading…'}
        </span>
      </div>

      {error && (
        <p className="rounded border border-[color-mix(in_srgb,var(--danger)_40%,transparent)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] p-2 text-xs text-danger" role="alert">
          {error}
        </p>
      )}

      {!permissions ? (
        <p className="text-xs text-muted">Loading permissions…</p>
      ) : (
        <div className="space-y-3">
          {editableKeys.length === 0 ? (
            <p className="text-xs text-muted">
              This kind exposes no editable fields. Its credential is managed through the
              sign-in / pairing flow instead.
            </p>
          ) : (
            editableKeys.map((key) => (
              <label key={key} className="block">
                <span className="flex items-center gap-2 text-xs font-medium text-primary">
                  {key}
                  {key === 'ssl' && (
                    <span className="text-[10px] font-normal text-muted">
                      (boolean — leave empty to clear)
                    </span>
                  )}
                </span>
                <input
                  type={key === 'port' ? 'number' : key === 'ssl' ? 'checkbox' : 'text'}
                  value={fields[key] ?? ''}
                  disabled={saving}
                  onChange={(e) =>
                    setFields((prev) => ({
                      ...prev,
                      [key]:
                        key === 'ssl'
                          ? e.target.checked
                            ? 'true'
                            : 'false'
                          : e.target.value,
                    }))
                  }
                  className="mt-1 w-full max-w-xs rounded-md border border-line bg-elevated px-2.5 py-1.5 text-sm text-primary placeholder:text-muted disabled:opacity-50"
                  placeholder={FIELD_HINTS[key] ?? ''}
                  aria-label={`Editable field ${key}`}
                />
                {FIELD_HINTS[key] && key !== 'ssl' && (
                  <span className="mt-0.5 block text-[11px] text-muted">{FIELD_HINTS[key]}</span>
                )}
              </label>
            ))
          )}

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={save}
              disabled={saving || editableKeys.length === 0}
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save settings'}
            </button>
            {result && (
              <span className="text-xs text-muted" aria-live="polite">
                Applied: {result.applied.length > 0 ? result.applied.join(', ') : 'none'}
                {result.ignored.length > 0 && ` · ignored: ${result.ignored.join(', ')}`}
              </span>
            )}
          </div>

          {permissions.denied_keys.length > 0 && (
            <p className="text-[11px] text-muted">
              Never editable (stored encrypted in the vault, or provider-managed):{' '}
              <code className="font-mono">{permissions.denied_keys.join(', ')}</code>
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function errMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'detail' in err && typeof (err as { detail: unknown }).detail === 'string') {
    return (err as { detail: string }).detail
  }
  if (err instanceof Error && err.message) return err.message
  return 'request failed'
}
