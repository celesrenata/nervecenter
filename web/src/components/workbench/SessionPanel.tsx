// Session panel: streamed messages, tool-call summaries, message box, abort, model selector
// (Req 3.6-3.10, 3.12, 8.5, 8.6).
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  streamSessionEvents, workbenchApi,
  type WorkbenchSessionEvent,
} from '../../api/workbench'
import { isLocalRoute } from './isLocalRoute'

export const MESSAGE_MAX = 20_000

export type MessageError = 'empty' | 'too_long' | 'not_running' | null

export function validateMessage(text: string, running: boolean): MessageError {
  if (!running) return 'not_running'
  if (text.length < 1) return 'empty'
  if (text.length > MESSAGE_MAX) return 'too_long'
  return null
}

const MESSAGE_ERRORS: Record<Exclude<MessageError, null>, string> = {
  empty: 'Message cannot be empty.',
  too_long: `Message is longer than ${MESSAGE_MAX} characters.`,
  not_running: 'The Session is not running.',
}

interface Entry {
  id: number
  kind: 'message' | 'tool'
  text: string
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {}
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v ? v : undefined
}

/** Reduce one SSE event to a display entry (or null for events with nothing to show). */
export function eventToEntry(ev: WorkbenchSessionEvent): Omit<Entry, 'id'> | null {
  const name = (str(asRecord(ev.data).type) ?? ev.event).toLowerCase()
  if (name === 'file_changed' || name.includes('heartbeat')) return null
  const outer = asRecord(ev.data)
  const data = asRecord(outer.data ?? outer.properties ?? outer)
  if (name.includes('tool')) {
    const tool = str(data.tool) ?? str(data.name) ?? 'tool'
    const status = str(asRecord(data.state).status) ?? str(data.status)
    const target = str(data.path) ?? str(data.file) ?? str(asRecord(data.input).path)
    return {
      kind: 'tool',
      text: [tool, target, status ? `(${status})` : null].filter(Boolean).join(' '),
    }
  }
  const text =
    str(data.text) ?? str(data.content) ?? str(data.delta) ?? str(outer.text) ??
    (typeof ev.data === 'string' && ev.data ? ev.data : undefined)
  return text ? { kind: 'message', text } : null
}

export interface SessionPanelProps {
  sessionId: string
  /** Whether the Session is currently running (messages are rejected otherwise). */
  running: boolean
  model: string
  /** Models offered in the selector. */
  models?: string[]
  /** Called with the confirmed model; resolve to apply it, reject to keep the previous one. */
  onModelChange?: (model: string, confirmNonlocal: boolean) => Promise<void> | void
  onFileChanged?: (path: string) => void
}

export default function SessionPanel({
  sessionId, running, model, models = [], onModelChange, onFileChanged,
}: SessionPanelProps) {
  const [entries, setEntries] = useState<Entry[]>([])
  const [text, setText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const [selected, setSelected] = useState(model)
  const nextId = useRef(0)
  const fileCb = useRef(onFileChanged)
  useEffect(() => { fileCb.current = onFileChanged }, [onFileChanged])
  useEffect(() => { setSelected(model) }, [model])

  useEffect(() => {
    const ctrl = new AbortController()
    setUnavailable(false)
    streamSessionEvents(sessionId, (ev) => {
      const d = asRecord(ev.data)
      if (ev.event === 'file_changed' || d.type === 'file_changed') {
        const p = str(d.path)
        if (p) fileCb.current?.(p)
        return
      }
      const entry = eventToEntry(ev)
      if (entry) setEntries((prev) => [...prev, { id: nextId.current++, ...entry }])
    }, ctrl.signal).catch((e: unknown) => {
      if (ctrl.signal.aborted) return
      const status = asRecord(e).status
      if (status === 503) setUnavailable(true)
      else setError(e instanceof Error ? e.message : 'Event stream failed')
    })
    return () => ctrl.abort()
  }, [sessionId])

  const send = useCallback(async () => {
    const invalid = validateMessage(text, running)
    if (invalid) {
      setError(MESSAGE_ERRORS[invalid])
      return // keep the entered text
    }
    try {
      await workbenchApi.sendMessage(sessionId, text)
      setError(null)
      setText('')
    } catch (e) {
      if (asRecord(e).status === 503) setUnavailable(true)
      setError(e instanceof Error ? e.message : 'Message was not sent')
    }
  }, [sessionId, text, running])

  const abort = async () => {
    try {
      await workbenchApi.abortSession(sessionId)
      setError(null)
    } catch (e) {
      if (asRecord(e).status === 503) setUnavailable(true)
      setError(e instanceof Error ? e.message : 'Abort failed')
    }
  }

  const changeModel = async (next: string) => {
    const nonLocal = !isLocalRoute(next)
    if (nonLocal && !window.confirm(`${next} is a non-local model. Use it for this Session?`)) {
      setSelected(selected) // declined: keep the previous selection
      return
    }
    const previous = selected
    setSelected(next)
    try {
      await onModelChange?.(next, nonLocal)
      setError(null)
    } catch (e) {
      setSelected(previous)
      setError(e instanceof Error ? e.message : 'Model change failed')
    }
  }

  const options = models.includes(selected) ? models : [selected, ...models].filter(Boolean)

  return (
    <section aria-label="Session" className="flex h-full flex-col gap-2">
      {unavailable && (
        <div role="status" data-testid="opencode-unavailable">
          opencode unavailable. Projects and the editor still work; try again shortly.
        </div>
      )}
      <div className="flex items-center gap-2">
        <label>
          Model{' '}
          <select
            aria-label="Model"
            value={selected}
            onChange={(e) => void changeModel(e.target.value)}
          >
            {options.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        {!isLocalRoute(selected) && <span data-testid="nonlocal-label">non-local</span>}
        <button type="button" onClick={() => void abort()} disabled={!running}>Abort</button>
      </div>
      <ol aria-label="Session messages" className="flex-1 overflow-auto">
        {entries.map((e) => (
          <li key={e.id} data-kind={e.kind}>
            {e.kind === 'tool' ? <code>{e.text}</code> : e.text}
          </li>
        ))}
      </ol>
      {error && <div role="alert">{error}</div>}
      <form onSubmit={(e) => { e.preventDefault(); void send() }}>
        <textarea
          aria-label="Message"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button type="submit">Send</button>
      </form>
    </section>
  )
}
