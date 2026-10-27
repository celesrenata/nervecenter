/**
 * Connection Center — container view (spec `personal-ops-recovery` §3,
 * workstream A).
 *
 * Owns the connection list state and implements every
 * `ConnectionCardActions` callback against the frozen `/api/connections`
 * client, re-reading the list after each action so the card's two badges,
 * error line and import progress always mirror the backend's recorded
 * state. Per-card action feedback (success / failed action / degraded
 * write) is surfaced as a small notice above the card's actions — the
 * stable error code + plain-language recovery step itself lives in the
 * card's diagnostics panel (acceptance row 13).
 *
 * Degradation (Area 4 / row 14): when no backend is connected the list is
 * the honest empty list — connections are never fabricated — and a
 * visible banner says so.
 */

import { useCallback, useEffect, useState } from 'react'
import {
  connectionsApi,
  isDegradedWrite,
  type Connection,
} from '../api/connections'
import { useMockMode } from '../api/client'
import { ConnectionCard, type CardNotice } from '../components/connections/ConnectionCard'
import { AddConnectionWizard } from '../components/connections/AddConnectionWizard'

/** Per-card transient feedback, keyed by connection id. */
type Notices = Record<string, CardNotice | null>

function errText(err: unknown): string {
  if (err && typeof err === 'object' && 'detail' in err) {
    const detail = (err as { detail: unknown }).detail
    if (typeof detail === 'string' && detail !== '') return detail
  }
  if (err instanceof Error && err.message !== '') return err.message
  return 'Something went wrong — please retry.'
}

/** A failed probe/sync/import surfaces the stable code and points to diagnostics. */
function failureNotice(action: string, code: string, message?: string): CardNotice {
  return {
    tone: 'danger',
    text:
      `${action} failed — ${code}` +
      (message ? `: ${message}` : '') +
      '. Open diagnostics for the recovery step.',
  }
}

export function ConnectionsView() {
  const [connections, setConnections] = useState<Connection[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [showWizard, setShowWizard] = useState(false)
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(new Set())
  const [notices, setNotices] = useState<Notices>({})

  /** Re-read the list; prunes notices for rows that no longer exist. */
  const fetchList = useCallback(async (): Promise<void> => {
    const { data } = await connectionsApi.list()
    setConnections(data)
    setNotices((prev) => {
      const ids = new Set(data.map((c) => c.id))
      const next: Notices = {}
      for (const [id, note] of Object.entries(prev)) if (ids.has(id) && note) next[id] = note
      return next
    })
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    fetchList()
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(errText(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [fetchList])

  function setBusy(id: string, busy: boolean) {
    setBusyIds((prev) => {
      const next = new Set(prev)
      if (busy) next.add(id)
      else next.delete(id)
      return next
    })
  }

  /**
   * Run one card action: mark the card busy, clear its old notice, perform
   * the API call, then refresh the list (so badges/error lines update from
   * the row itself). Errors — network failures, 4xx/5xx, and honest
   * `executed:false` degraded writes — become a danger notice on the card.
   */
  async function perform(
    connection: Connection,
    operation: (row: Connection) => Promise<CardNotice | null>,
  ): Promise<void> {
    if (busyIds.has(connection.id)) return
    setBusy(connection.id, true)
    setNotices((prev) => ({ ...prev, [connection.id]: null }))
    try {
      const notice = await operation(connection)
      if (notice) setNotices((prev) => ({ ...prev, [connection.id]: notice }))
      await fetchList()
    } catch (err) {
      setNotices((prev) => ({
        ...prev,
        [connection.id]: { tone: 'danger', text: errText(err) },
      }))
    } finally {
      setBusy(connection.id, false)
    }
  }

  const actions = {
    onToggleEnabled: (c: Connection, enabled: boolean) =>
      perform(c, async (row) => {
        const res = await connectionsApi.update(row.id, { enabled })
        if (isDegradedWrite(res)) throw new Error(res.detail)
        return null
      }),

    onVerify: (c: Connection) =>
      perform(c, async (row) => {
        const res = await connectionsApi.verify(row.id)
        if (isDegradedWrite(res)) throw new Error(res.detail)
        if (res.ok === false) return failureNotice('Verify', res.error_code, res.error_message)
        return {
          tone: 'success',
          text: 'Login probe passed — authentication confirmed. The operational axis only turns Healthy once data is actually ingested.',
        }
      }),

    onSyncNow: (c: Connection) =>
      perform(c, async (row) => {
        const res = await connectionsApi.syncNow(row.id)
        if (isDegradedWrite(res)) throw new Error(res.detail)
        if (res.ok === false) return failureNotice('Sync', res.error_code)
        return { tone: 'success', text: 'Sync started — progress appears on this card.' }
      }),

    onImport: (c: Connection) =>
      perform(c, async (row) => {
        const res = await connectionsApi.startImport(row.id)
        if (isDegradedWrite(res)) throw new Error(res.detail)
        if (res.ok === false) return failureNotice('Import', res.error_code)
        return {
          tone: 'success',
          text: 'Initial import started as a background job — progress appears on this card.',
        }
      }),

    onReconnect: (c: Connection) =>
      perform(c, async (row) => {
        const res = await connectionsApi.reconnect(row.id)
        if (isDegradedWrite(res)) throw new Error(res.detail)
        if (res.ok === false) return failureNotice('Reconnect', res.error_code)
        return { tone: 'success', text: 'Reconnected — re-verified the login and re-synced.' }
      }),

    onRemove: (c: Connection) =>
      perform(c, async (row) => {
        await connectionsApi.remove(row.id)
        return null
      }),

    // The card toggles its panels locally; these hooks refresh the row the
    // view owns so panel refresh keys change when the backend moves on.
    onShowDiagnostics: () => {
      void fetchList()
    },
    onShowPermissions: (updated: Connection) => {
      setConnections((prev) => prev.map((c) => (c.id === updated.id ? updated : c)))
    },
  }

  function closeWizardAndRefresh() {
    setShowWizard(false)
    void fetchList()
  }

  return (
    <div className="space-y-6">
      {/* Header: purpose + dominant primary action */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold text-primary">Connection Center</h1>
          <p className="text-sm text-muted mt-1">
            The sources NerveCenter ingests — per-connection status, actions, and recovery.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowWizard(true)}
          disabled={loading}
          className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
        >
          Add connection
        </button>
      </div>

      {useMockMode && (
        <div className="rounded-md border border-line bg-hover/40 p-4 text-sm text-secondary">
          No backend is connected to this build (mock mode). The list below is intentionally
          empty — connections are never fabricated — and setup writes report
          <code className="text-xs"> executed: false</code> instead of pretending to succeed.
        </div>
      )}

      {showWizard && (
        <AddConnectionWizard onCreated={closeWizardAndRefresh} onCancel={() => setShowWizard(false)} />
      )}

      {loading ? (
        <div
          className="rounded-md border border-line bg-elevated p-6 text-sm text-muted"
          role="status"
        >
          Loading connections…
        </div>
      ) : loadError ? (
        <div
          className="rounded-md border border-[color-mix(in_srgb,var(--danger)_40%,transparent)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] p-4 text-sm text-danger"
          role="alert"
        >
          <p>Could not load connections: {loadError}</p>
          <button
            type="button"
            onClick={() => {
              setLoading(true)
              setLoadError(null)
              fetchList()
                .catch((err: unknown) => setLoadError(errText(err)))
                .finally(() => setLoading(false))
            }}
            className="mt-3 rounded-md border border-line px-3 py-1.5 text-sm text-secondary hover:bg-hover"
          >
            Retry
          </button>
        </div>
      ) : connections.length === 0 ? (
        <div className="rounded-md border border-line bg-elevated p-8 text-center">
          <p className="text-sm font-medium text-primary">No connections yet</p>
          <p className="text-sm text-secondary mt-1">
            Add a mailbox, calendar, or relay device to start ingesting. Setup walks through
            provider sign-in, verification, and the initial import.
          </p>
          <button
            type="button"
            onClick={() => setShowWizard(true)}
            className="mt-4 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover"
          >
            Add your first connection
          </button>
        </div>
      ) : (
        <div className="space-y-4">
          {connections.map((connection) => (
            <ConnectionCard
              key={connection.id}
              connection={connection}
              busy={busyIds.has(connection.id)}
              notice={notices[connection.id] ?? null}
              actions={actions}
            />
          ))}
        </div>
      )}
    </div>
  )
}
