/**
 * Streaming Ask client — `POST /api/context/ask/stream` (SSE).
 *
 * Events: meta {request_id,session_id}, delta {text}, proposal {...},
 * done {full ask body, same shape as POST /api/context/ask}, error {code,message},
 * status {code:'retrying', message, attempt, max_attempts, retry_in_s} while a busy model is retried.
 * A non-2xx response rejects with `ApiError` (status + detail), so the caller
 * can tell an expired session (401) from a validation error (422) or a dead
 * network (TypeError from fetch).
 */

import { apiFetch, apiErrorFromResponse, askApi, useMockMode } from './client'
import type { CalendarProposal } from './calendar'

export interface AskStreamBody {
  question: string
  agent?: string
  inference?: { model?: string; provider?: string }
  account_id?: string
  session_id?: string
}

export type AskStreamEvent =
  | { type: 'meta'; data: { request_id?: string; session_id?: string } }
  | { type: 'delta'; data: { text: string } }
  | { type: 'proposal'; data: CalendarProposal }
  | { type: 'done'; data: Record<string, unknown> }
  | { type: 'error'; data: { code?: string; message?: string } }
  | {
      type: 'status'
      data: { code?: string; message?: string; attempt?: number; max_attempts?: number; retry_in_s?: number }
    }

const KNOWN_EVENTS = new Set(['meta', 'delta', 'proposal', 'done', 'error', 'status'])

/** Parse one SSE frame (the text between blank lines) into an event, or null. */
export function parseSseFrame(frame: string): AskStreamEvent | null {
  let name = 'message'
  const dataLines: string[] = []
  for (const rawLine of frame.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) name = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (!KNOWN_EVENTS.has(name) || dataLines.length === 0) return null
  try {
    return { type: name, data: JSON.parse(dataLines.join('\n')) } as AskStreamEvent
  } catch {
    return null
  }
}

export async function postAskStream(
  body: AskStreamBody,
  onEvent: (event: AskStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (useMockMode) {
    // No backend: the in-browser mock answers in one piece.
    const res = await askApi.post(body.question)
    onEvent({ type: 'done', data: (res.data ?? {}) as Record<string, unknown> })
    return
  }
  const res = await apiFetch('/api/context/ask/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) throw await apiErrorFromResponse(res)
  if (!res.body) {
    throw new Error('The server answered without a stream body')
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const drain = (final: boolean) => {
    buffer = buffer.replace(/\r\n/g, '\n')
    let idx = buffer.indexOf('\n\n')
    while (idx !== -1) {
      const ev = parseSseFrame(buffer.slice(0, idx))
      buffer = buffer.slice(idx + 2)
      if (ev) onEvent(ev)
      idx = buffer.indexOf('\n\n')
    }
    if (final && buffer.trim().length > 0) {
      const ev = parseSseFrame(buffer)
      buffer = ''
      if (ev) onEvent(ev)
    }
  }
  for (;;) {
    const { value, done } = await reader.read()
    if (value) buffer += decoder.decode(value, { stream: !done })
    drain(done)
    if (done) break
  }
}
