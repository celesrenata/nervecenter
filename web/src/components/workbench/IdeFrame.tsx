// Browser_IDE controls and iframe (Req 5.7, 5.10, 5.11, 5.12).
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  workbenchApi,
  type WorkbenchIdeInstance,
  type WorkbenchKind,
} from '../../api/workbench'

export const IDE_POLL_MS = 2000

export interface IdeFrameProps {
  kind: WorkbenchKind
  slug: string
  /** When provided, skips the workbenchApi.config() lookup. */
  ideEnabled?: boolean
  pollMs?: number
}

const messageOf = (e: unknown): string => {
  const detail = (e as { detail?: unknown })?.detail
  if (typeof detail === 'string' && detail) return detail
  return e instanceof Error && e.message ? e.message : 'Request failed'
}

export function ideIframeSrc(slug: string): string {
  return `/ide/${encodeURIComponent(slug)}/`
}

export default function IdeFrame({ kind, slug, ideEnabled, pollMs = IDE_POLL_MS }: IdeFrameProps) {
  const [enabled, setEnabled] = useState<boolean | undefined>(ideEnabled)
  const [instance, setInstance] = useState<WorkbenchIdeInstance | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  useEffect(() => {
    if (ideEnabled !== undefined) {
      setEnabled(ideEnabled)
      return
    }
    let cancelled = false
    workbenchApi
      .config()
      .then((c) => { if (!cancelled) setEnabled(c.ide_enabled === true) })
      .catch(() => { if (!cancelled) setEnabled(false) })
    return () => { cancelled = true }
  }, [ideEnabled])

  const refresh = useCallback(async () => {
    const { instances } = await workbenchApi.listIdeInstances()
    if (!alive.current) return null
    const mine = instances.find((i) => i.kind === kind && i.slug === slug) ?? null
    setInstance(mine)
    return mine
  }, [kind, slug])

  // Load the current instance once the IDE is known to be enabled.
  useEffect(() => {
    if (!enabled) return
    refresh().catch((e) => alive.current && setError(messageOf(e)))
  }, [enabled, refresh])

  // Poll until the instance is no longer starting.
  const state = instance?.state
  useEffect(() => {
    if (!enabled || state !== 'starting') return
    const t = setInterval(() => {
      refresh().catch((e) => alive.current && setError(messageOf(e)))
    }, pollMs)
    return () => clearInterval(t)
  }, [enabled, state, refresh, pollMs])

  // The iframe cannot send the bearer token, so get the path-scoped nc_ide
  // cookie first and renew it well before its 8 h expiry.
  const [ticketReady, setTicketReady] = useState(false)
  useEffect(() => {
    if (!enabled || state !== 'running') {
      setTicketReady(false)
      return
    }
    const issue = () =>
      workbenchApi
        .ideTicket(kind, slug)
        .then(() => alive.current && setTicketReady(true))
        .catch((e) => alive.current && setError(messageOf(e)))
    issue()
    const t = setInterval(issue, 4 * 60 * 60 * 1000)
    return () => clearInterval(t)
  }, [enabled, state, kind, slug])
  const start = async () => {
    setBusy(true)
    setError(null)
    try {
      const inst = await workbenchApi.startIde(kind, slug)
      if (alive.current) setInstance(inst)
    } catch (e) {
      if (alive.current) setError(messageOf(e))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  const stop = async () => {
    setBusy(true)
    setError(null)
    try {
      const inst = await workbenchApi.stopIde(kind, slug)
      if (alive.current) setInstance(inst)
    } catch (e) {
      if (alive.current) setError(messageOf(e))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  if (!enabled) return null

  const failed = state === 'failed'
  const active = state === 'starting' || state === 'running' || state === 'stopping'

  return (
    <section aria-label="Browser IDE" className="space-y-2">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium">Browser IDE</h3>
        {state && <span className="text-xs opacity-70">{state}</span>}
        {!active && (
          <button type="button" onClick={start} disabled={busy}>
            Start IDE
          </button>
        )}
        {(state === 'starting' || state === 'running') && (
          <button type="button" onClick={stop} disabled={busy}>
            Stop IDE
          </button>
        )}
      </div>
      {state === 'starting' && <p role="status">Starting the IDE…</p>}
      {failed && (
        <div role="alert">
          IDE failed to start{instance?.reason ? `: ${instance.reason}` : '.'}
        </div>
      )}
      {error && <div role="alert">{error}</div>}
      {state === 'running' && ticketReady && (
        <iframe
          title="Browser IDE"
          src={ideIframeSrc(slug)}
          className="w-full h-[70vh] border-0"
        />
      )}
    </section>
  )
}
