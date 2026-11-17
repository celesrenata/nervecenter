/**
 * Ask sessions live here, outside the React tree, so a conversation and any
 * answer still streaming survive leaving the Ask page. Each session is a tab.
 *
 * Chats are saved to the account (``PUT /api/ask/sessions``, debounced) so they
 * survive closing the browser and follow the account to other devices, and
 * mirrored to sessionStorage so a reload shows them before the server answers.
 * An answer that was streaming during a reload cannot resume and is marked as
 * interrupted.
 */
import { ApiError, apiFetch } from '../../api/client'
import { postAskStream } from '../../api/askStream'
import { voiceApi, VoiceApiError } from '../../api/voice'
import type { CalendarProposal } from '../../api/calendar'
import { SpeechQueue } from '../../voice/SpeechQueue'

export interface EvidenceItem {
  statement: string
  type: 'FACT' | 'HYPOTHESIS' | 'OBSERVATION' | 'CLAIM' | 'DERIVED_FACT' | 'RECOMMENDATION'
  confidence: number
  evidence: string[]
}

interface AskResponse {
  answer: string
  confidence: number
  facts: EvidenceItem[]
  hypotheses: EvidenceItem[]
  reasoning?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: string
  provenance?: { facts: EvidenceItem[]; hypotheses: EvidenceItem[]; reasoning?: string }
  proposal?: CalendarProposal
  /** Event Ask created directly (confirm-first off); offers Undo. */
  undoEventId?: string
  /** Transient progress while the server waits on a busy model. */
  status?: string
}

export interface AskSession {
  id: string
  title: string
  messages: ChatMessage[]
  input: string
  loading: boolean
  speechError: string | null
  /** ISO time of the last change; the newer copy wins when merging. */
  updatedAt?: string
}

export interface AskOverrides {
  agent?: string
  model?: string
  provider?: string
}

const STORAGE_KEY = 'nerve.ask.sessions.v1'
const GREETING = 'Hello! I am NerveCenter. How can I help you today?'
const TITLE_MAX = 32

/** Stable speech error codes → text. Speech never blocks the text answer. */
export function describeSpeechError(err: unknown): string {
  if (err instanceof VoiceApiError) {
    switch (err.code) {
      case 'speech_unavailable':
        return 'The speech service is unavailable right now. Text answers still work.'
      case 'bad_audio':
        return 'The recording could not be understood. Try again, closer to the microphone.'
      case 'speech_policy':
        return 'Speech is misconfigured on the server (model not allowed).'
      case 'unknown_voice':
        return 'The selected voice is not available. Pick another in Settings → Voice.'
      default:
        if (err.status === 413) return 'The recording is too long to transcribe.'
        if (err.status === 415) return 'This browser recorded an audio format the server does not accept.'
        return `Speech failed (${err.code}).`
    }
  }
  if (err instanceof ApiError && err.status === 401) return 'Your session expired. Sign in again.'
  if (err instanceof TypeError) return 'Could not reach the server for speech.'
  return 'Speech failed.'
}

/** Specific text for a failed Ask request; never the generic apology. */
export function describeAskError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return 'Your session expired. Sign in again to keep asking.'
    if (err.status === 403) return `You are not allowed to ask this: ${err.detail}`
    if (err.status === 422) return `The request was rejected as invalid: ${err.detail}`
    if (err.status === 429) return 'Too many requests. Wait a moment and try again.'
    if (err.status >= 500) return `The server failed to answer (HTTP ${err.status}): ${err.detail}`
    return `The request failed (HTTP ${err.status}): ${err.detail}`
  }
  if (err instanceof TypeError) return 'Could not reach the server. Check your connection and try again.'
  return `Ask failed: ${err instanceof Error ? err.message : String(err)}`
}

function newId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `s-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function freshSession(): AskSession {
  return {
    id: newId(),
    title: 'New chat',
    messages: [{ id: newId(), role: 'assistant', content: GREETING, timestamp: new Date().toISOString() }],
    input: '',
    loading: false,
    speechError: null,
  }
}

function titleFrom(question: string): string {
  const t = question.replace(/\s+/g, ' ').trim()
  return t.length > TITLE_MAX ? `${t.slice(0, TITLE_MAX - 1)}…` : t || 'New chat'
}

interface State {
  sessions: AskSession[]
  activeId: string
}

function load(): State {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as State
      if (Array.isArray(parsed.sessions) && parsed.sessions.length > 0) {
        const sessions = parsed.sessions.map((s) => ({
          ...s,
          speechError: null,
          // A stream cannot survive a reload; say so instead of spinning forever.
          messages: s.loading
            ? s.messages.map((m, i) =>
                i === s.messages.length - 1 && m.role === 'assistant'
                  ? { ...m, content: m.content ? `${m.content}\n\n(Interrupted by a page reload.)` : 'Interrupted by a page reload.' }
                  : m,
              )
            : s.messages,
          loading: false,
        }))
        const activeId = sessions.some((s) => s.id === parsed.activeId) ? parsed.activeId : sessions[0].id
        return { sessions, activeId }
      }
    }
  } catch {
    /* unreadable storage: start fresh */
  }
  const s = freshSession()
  return { sessions: [s], activeId: s.id }
}

let state: State = load()
const listeners = new Set<() => void>()
const aborts = new Map<string, AbortController>()
let queue: SpeechQueue | null = null
let queueSession: string | null = null

/** Spoken-reply preference and voice, set by the view from account settings. */
export const voicePrefs = { spoken: false, voice: undefined as string | undefined }

function persist(): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    /* storage full or unavailable: in-memory sessions still work */
  }
}

const SAVE_DEBOUNCE_MS = 1500
let saveTimer: ReturnType<typeof setTimeout> | null = null
let hydrated = false

/** Server copy: no draft input, no transient flags. */
function serverDocument() {
  return {
    active_id: state.activeId,
    sessions: state.sessions
      .filter((s) => s.messages.some((m) => m.role === 'user'))
      .slice(-50)
      .map((s) => ({
        id: s.id,
        title: s.title,
        updated_at: s.updatedAt ?? null,
        messages: s.messages.slice(-400).map((m) => ({ ...m, content: m.content.slice(0, 50_000) })),
      })),
  }
}

async function saveNow(): Promise<void> {
  saveTimer = null
  try {
    await apiFetch('/api/ask/sessions', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serverDocument()),
    })
  } catch {
    /* offline: the next change retries; sessionStorage still has the chats */
  }
}

function scheduleSave(): void {
  if (!hydrated) return
  if (saveTimer !== null) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => void saveNow(), SAVE_DEBOUNCE_MS)
}

function set(next: State, opts: { save?: boolean } = {}): void {
  state = next
  persist()
  if (opts.save !== false) scheduleSave()
  listeners.forEach((l) => l())
}

function patchSession(id: string, fn: (s: AskSession) => AskSession, opts: { save?: boolean } = {}): void {
  set(
    {
      ...state,
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...fn(s), ...(opts.save === false ? {} : { updatedAt: new Date().toISOString() }) } : s,
      ),
    },
    opts,
  )
}

interface ServerSession {
  id: string
  title: string
  messages: ChatMessage[]
  updated_at?: string | null
}

/** Union by id; for a session on both sides the newer copy wins. */
function merge(local: AskSession[], remote: ServerSession[]): AskSession[] {
  const byId = new Map(local.map((s) => [s.id, s]))
  for (const r of remote) {
    const mine = byId.get(r.id)
    if (mine && (mine.loading || (mine.updatedAt ?? '') >= (r.updated_at ?? ''))) continue
    byId.set(r.id, {
      id: r.id,
      title: r.title,
      messages: r.messages,
      input: mine?.input ?? '',
      loading: false,
      speechError: null,
      updatedAt: r.updated_at ?? undefined,
    })
  }
  const merged = [...byId.values()]
  // Drop the untouched greeting-only tab when saved chats exist.
  const used = merged.filter((s) => s.loading || s.input || s.messages.some((m) => m.role === 'user'))
  return used.length > 0 ? used : merged.slice(0, 1)
}

function patchMessage(sessionId: string, messageId: string, fn: (m: ChatMessage) => ChatMessage): void {
  patchSession(sessionId, (s) => ({ ...s, messages: s.messages.map((m) => (m.id === messageId ? fn(m) : m)) }))
}

export const askSessions = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
  getState(): State {
    return state
  },
  activate(id: string): void {
    if (state.sessions.some((s) => s.id === id)) set({ ...state, activeId: id })
  },
  create(): string {
    const s = freshSession()
    set({ sessions: [...state.sessions, s], activeId: s.id })
    return s.id
  },
  close(id: string): void {
    aborts.get(id)?.abort()
    aborts.delete(id)
    if (queueSession === id) {
      queue?.stop()
      queue = null
      queueSession = null
    }
    const idx = state.sessions.findIndex((s) => s.id === id)
    const rest = state.sessions.filter((s) => s.id !== id)
    if (rest.length === 0) {
      const s = freshSession()
      set({ sessions: [s], activeId: s.id })
      return
    }
    const activeId = state.activeId === id ? rest[Math.max(0, idx - 1)].id : state.activeId
    set({ sessions: rest, activeId })
  },
  setInput(id: string, input: string): void {
    patchSession(id, (s) => ({ ...s, input }), { save: false })
  },
  setSpeechError(id: string, speechError: string | null): void {
    patchSession(id, (s) => ({ ...s, speechError }), { save: false })
  },
  /** Load the account's saved chats once (the Ask view calls this on mount). */
  async hydrate(): Promise<void> {
    if (hydrated) return
    try {
      const res = await apiFetch('/api/ask/sessions')
      if (res.ok) {
        const doc = (await res.json()) as { sessions?: ServerSession[]; active_id?: string | null }
        const remote = Array.isArray(doc.sessions) ? doc.sessions : []
        if (remote.length > 0) {
          const sessions = merge(state.sessions, remote)
          const activeId = sessions.some((x) => x.id === state.activeId)
            ? state.activeId
            : sessions.some((x) => x.id === doc.active_id)
              ? (doc.active_id as string)
              : sessions[0].id
          hydrated = true
          set({ sessions, activeId })
          return
        }
      }
    } catch {
      /* offline: keep the local copy; saving starts anyway */
    }
    hydrated = true
    scheduleSave()
  },
  /** Mute/unmute whatever is speaking now (the server is told per session). */
  setSpoken(spoken: boolean): void {
    voicePrefs.spoken = spoken
    queue?.setMuted(!spoken)
    for (const s of state.sessions) {
      if (s.id === queueSession || aborts.has(s.id)) {
        void voiceApi.setMuted(s.id, !spoken).catch((e) => askSessions.setSpeechError(s.id, describeSpeechError(e)))
      }
    }
  },
  async send(sessionId: string, text: string, overrides: AskOverrides): Promise<void> {
    const session = state.sessions.find((s) => s.id === sessionId)
    const question = text.trim()
    if (!session || !question || session.loading) return

    const now = new Date().toISOString()
    const assistantId = newId()
    patchSession(sessionId, (s) => ({
      ...s,
      title: s.messages.some((m) => m.role === 'user') ? s.title : titleFrom(question),
      input: '',
      loading: true,
      speechError: null,
      messages: [
        ...s.messages,
        { id: newId(), role: 'user', content: question, timestamp: now },
        { id: assistantId, role: 'assistant', content: '', timestamp: now },
      ],
    }))

    // One voice at a time: a new ask anywhere stops whatever was speaking.
    queue?.stop()
    const abort = new AbortController()
    aborts.set(sessionId, abort)
    const q = new SpeechQueue({
      speak: (sentence, signal) =>
        voiceApi.speak({ text: sentence, voice: voicePrefs.voice, session_id: sessionId }, signal),
      onError: (e) => askSessions.setSpeechError(sessionId, describeSpeechError(e)),
      startMuted: !voicePrefs.spoken,
    })
    queue = q
    queueSession = sessionId
    const patch = (fn: (m: ChatMessage) => ChatMessage) => patchMessage(sessionId, assistantId, fn)

    let finished = false
    try {
      const inference =
        overrides.model || overrides.provider
          ? {
              ...(overrides.model ? { model: overrides.model } : {}),
              ...(overrides.provider ? { provider: overrides.provider } : {}),
            }
          : undefined
      await postAskStream(
        {
          question,
          session_id: sessionId,
          ...(overrides.agent ? { agent: overrides.agent } : {}),
          ...(inference ? { inference } : {}),
        },
        (ev) => {
          if (ev.type === 'status') {
            patch((m) => ({ ...m, status: ev.data.message ?? 'Waiting for the model…' }))
          } else if (ev.type === 'delta') {
            q.push(ev.data.text)
            patch((m) => ({ ...m, status: undefined, content: m.content + ev.data.text }))
          } else if (ev.type === 'proposal') {
            patch((m) => ({ ...m, proposal: ev.data }))
          } else if (ev.type === 'done') {
            finished = true
            q.end()
            const data = ev.data as unknown as AskResponse & {
              accepted?: boolean
              executed?: boolean
              detail?: string
              reason?: string
              status?: string
              policy?: { reason?: string }
              undo?: { event_id?: string }
            }
            const undoEventId =
              data.status === 'created' && typeof data.undo?.event_id === 'string' ? data.undo.event_id : undefined
            let content: string
            if (typeof data.answer === 'string' && data.answer.length > 0) {
              content = data.answer
            } else if (data.status === 'policy_denied') {
              content = `This request was denied by policy: ${
                data.reason || data.policy?.reason || data.detail || 'no reason given'
              }`
            } else if (data.accepted === true) {
              const why = data.status === 'unavailable' && data.reason ? `Inference unavailable: ${data.reason}. ` : ''
              content = `${why}Request accepted (execution: ${data.executed ? 'completed' : 'pending'}). ${
                data.detail || ''
              }`.trim()
            } else {
              content = 'The server finished without an answer.'
            }
            patch((m) => ({
              ...m,
              status: undefined,
              content,
              ...(undoEventId ? { undoEventId } : {}),
              provenance: Array.isArray(data.facts)
                ? {
                    facts: data.facts,
                    hypotheses: Array.isArray(data.hypotheses) ? data.hypotheses : [],
                    reasoning:
                      typeof data.confidence === 'number'
                        ? `Confidence: ${(data.confidence * 100).toFixed(0)}%`
                        : undefined,
                  }
                : undefined,
            }))
          } else if (ev.type === 'error') {
            finished = true
            q.stop()
            const code = ev.data.code ?? 'error'
            const msg = ev.data.message ?? ''
            const content =
              code === 'secure_route_denied'
                ? `The secure inference route refused this request: ${msg}`
                : code === 'stream_interrupted'
                  ? `The answer stream was interrupted${msg ? `: ${msg}` : ''}.`
                  : `Ask failed (${code})${msg ? `: ${msg}` : ''}`
            patch((m) => ({
              ...m,
              status: undefined,
              content: m.content ? `${m.content}\n\n${content}` : content,
            }))
          }
        },
        abort.signal,
      )
      if (!finished) {
        q.end()
        patch((m) => ({ ...m, content: m.content || 'The answer stream ended before any answer arrived.' }))
      }
    } catch (error) {
      if (abort.signal.aborted) return
      q.stop()
      const content = describeAskError(error)
      patch((m) => ({ ...m, content: m.content ? `${m.content}\n\n${content}` : content }))
    } finally {
      if (aborts.get(sessionId) === abort) {
        aborts.delete(sessionId)
        if (state.sessions.some((s) => s.id === sessionId)) patchSession(sessionId, (s) => ({ ...s, loading: false }))
      }
    }
  },
  /** Test hook: forget everything (aborts streams, clears storage). */
  reset(): void {
    if (saveTimer !== null) clearTimeout(saveTimer)
    saveTimer = null
    hydrated = false
    aborts.forEach((a) => a.abort())
    aborts.clear()
    queue?.stop()
    queue = null
    queueSession = null
    const s = freshSession()
    set({ sessions: [s], activeId: s.id })
  },
}
