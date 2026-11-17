import { useState, useEffect, useRef } from 'react'
import { ApiError } from '../api/client'
import { postAskStream } from '../api/askStream'
import { voiceApi, VoiceApiError } from '../api/voice'
import type { CalendarProposal } from '../api/calendar'
import { CalendarProposalCard, CreatedEventUndo } from '../components/CalendarProposalCard'
import { SpeechQueue } from '../voice/SpeechQueue'
import { useVoiceRecorder, isRecordingSupported } from '../voice/useVoiceRecorder'
import { inferenceApi, type AvailableInference } from '../api/inference'
import { agentsApi, type Agent } from '../api/agents'
import { useSettings } from '../theme/SettingsContext'

interface EvidenceItem {
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

interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: string
  provenance?: {
    facts: EvidenceItem[]
    hypotheses: EvidenceItem[]
    reasoning?: string
  }
  proposal?: CalendarProposal
  /** Event Ask created directly (confirm-first off); offers Undo. */
  undoEventId?: string
}

/** Stable speech error codes → text. Speech never blocks the text answer. */
function describeSpeechError(err: unknown): string {
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
function describeAskError(err: unknown): string {
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

function newSessionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `s-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const AskView = () => {
  const { settings, update } = useSettings()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)

  // Voice: per-mount session id (server mute registry key), spoken-reply
  // toggle + TTS voice (initialised from the per-account server settings).
  const sessionIdRef = useRef(newSessionId())
  const [spoken, setSpoken] = useState(false)
  const spokenRef = useRef(false)
  const voiceRef = useRef<string | undefined>(undefined)
  const [speechError, setSpeechError] = useState<string | null>(null)
  const queueRef = useRef<SpeechQueue | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const recorder = useVoiceRecorder()

  // INFER_CONTROL_CONTRACT.md (web scope): optional per-request inference +
  // agent control. The picker is COLLAPSED by default — "default" keeps the
  // service default — so the common path sends a bare `{question}` body and
  // pre-Batch-4 backends (AskRequest `extra: "forbid"`) keep working. Options
  // are fetched lazily, only when the user expands the picker, so a mount
  // issues no extra requests.
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerAgents, setPickerAgents] = useState<Agent[]>([])
  const [pickerAvailable, setPickerAvailable] = useState<AvailableInference | null>(null)
  const [pickerLoading, setPickerLoading] = useState(false)
  const [pickerError, setPickerError] = useState<string | null>(null)
  const [askAgent, setAskAgent] = useState('')
  // Prefill from the Settings → Inference defaults (browser-local); empty keeps
  // the service default. Changing a select here also syncs the default back.
  const [askModel, setAskModel] = useState(settings.askModel)
  const [askProvider, setAskProvider] = useState(settings.askProvider)

  const loadPickerOptions = async () => {
    setPickerLoading(true)
    setPickerError(null)
    try {
      const [agentsRes, availableRes] = await Promise.all([
        agentsApi.list().catch(() => null),
        inferenceApi.getAvailable().catch(() => null),
      ])
      if (agentsRes) setPickerAgents(agentsRes.data ?? [])
      if (availableRes) setPickerAvailable(availableRes)
    } catch {
      // Honest: a fetch failure leaves the selects empty + a message; the
      // default (no override) path is still fully usable.
      setPickerError('Could not load agent / model options.')
    } finally {
      setPickerLoading(false)
    }
  }

  const togglePicker = () => {
    const next = !pickerOpen
    setPickerOpen(next)
    if (next && !pickerLoading && pickerAvailable === null && pickerAgents.length === 0) {
      void loadPickerOptions()
    }
  }

  useEffect(() => {
    const initialMessages: ChatMessage[] = [
      {
        id: '1',
        role: 'assistant',
        content: 'Hello! I am NerveCenter. How can I help you today?',
        timestamp: new Date().toISOString(),
      },
    ]
    setMessages(initialMessages)
  }, [])

  useEffect(() => {
    let cancelled = false
    voiceApi
      .getSettings()
      .then((v) => {
        if (cancelled) return
        voiceRef.current = v.tts_voice
        spokenRef.current = v.spoken_replies
        setSpoken(v.spoken_replies)
      })
      .catch(() => {
        /* defaults stay: spoken replies off, server default voice */
      })
    return () => {
      cancelled = true
      // Leaving the page stops playback and any in-flight ask.
      queueRef.current?.stop()
      abortRef.current?.abort()
    }
  }, [])

  const toggleSpoken = () => {
    const next = !spokenRef.current
    spokenRef.current = next
    setSpoken(next)
    setSpeechError(null)
    queueRef.current?.setMuted(!next)
    void voiceApi.setMuted(sessionIdRef.current, !next).catch((e) => setSpeechError(describeSpeechError(e)))
    void voiceApi.putSettings({ spoken_replies: next }).catch((e) => setSpeechError(describeSpeechError(e)))
  }

  const handleMic = async () => {
    if (recorder.state === 'transcribing' || loading) return
    if (recorder.state === 'idle') {
      await recorder.start()
      return
    }
    const blob = await recorder.stop()
    if (!blob) {
      recorder.setError('Nothing was recorded. Try again.')
      return
    }
    recorder.setState('transcribing')
    try {
      const transcript = await voiceApi.transcribe(blob)
      recorder.setState('idle')
      const text = transcript.text.trim()
      if (!text) {
        setSpeechError('No speech was detected in the recording.')
        return
      }
      setSpeechError(null)
      void handleSendMessage(text)
    } catch (err) {
      recorder.setState('idle')
      setSpeechError(describeSpeechError(err))
    }
  }

  const handleSendMessage = async (override?: string) => {
    const text = (override ?? input).trim()
    if (!text || loading) return

    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      role: 'user',
      content: text,
      timestamp: new Date().toISOString(),
    }
    const assistantId = `${Date.now() + 1}`

    setMessages((prev) => [
      ...prev,
      userMessage,
      { id: assistantId, role: 'assistant', content: '', timestamp: new Date().toISOString() },
    ])
    setInput('')
    setLoading(true)
    setSpeechError(null)

    // A new ask stops whatever was still being spoken.
    queueRef.current?.stop()
    abortRef.current?.abort()
    const abort = new AbortController()
    abortRef.current = abort
    const queue = new SpeechQueue({
      speak: (sentence, signal) =>
        voiceApi.speak(
          { text: sentence, voice: voiceRef.current, session_id: sessionIdRef.current },
          signal,
        ),
      onError: (e) => setSpeechError(describeSpeechError(e)),
      startMuted: !spokenRef.current,
    })
    queueRef.current = queue

    const patch = (fn: (m: ChatMessage) => ChatMessage) =>
      setMessages((prev) => prev.map((m) => (m.id === assistantId ? fn(m) : m)))

    let finished = false
    try {
      // Optional per-request override (INFER_CONTROL_CONTRACT.md §2); omitted
      // when nothing is selected so the body stays the bare `{question}` shape.
      const inference =
        askModel || askProvider
          ? {
              ...(askModel ? { model: askModel } : {}),
              ...(askProvider ? { provider: askProvider } : {}),
            }
          : undefined
      await postAskStream(
        {
          question: text,
          session_id: sessionIdRef.current,
          ...(askAgent ? { agent: askAgent } : {}),
          ...(inference ? { inference } : {}),
        },
        (ev) => {
          if (ev.type === 'delta') {
            queue.push(ev.data.text)
            patch((m) => ({ ...m, content: m.content + ev.data.text }))
          } else if (ev.type === 'proposal') {
            patch((m) => ({ ...m, proposal: ev.data }))
          } else if (ev.type === 'done') {
            finished = true
            queue.end()
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
              data.status === 'created' && typeof data.undo?.event_id === 'string'
                ? data.undo.event_id
                : undefined
            let content: string
            if (typeof data.answer === 'string' && data.answer.length > 0) {
              content = data.answer
            } else if (data.status === 'policy_denied') {
              content = `This request was denied by policy: ${
                data.reason || data.policy?.reason || data.detail || 'no reason given'
              }`
            } else if (data.accepted === true) {
              const why = data.status === 'unavailable' && data.reason ? `Inference unavailable: ${data.reason}. ` : ''
              content = `${why}Request accepted (execution: ${
                data.executed ? 'completed' : 'pending'
              }). ${data.detail || ''}`.trim()
            } else {
              content = 'The server finished without an answer.'
            }
            patch((m) => ({
              ...m,
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
            queue.stop()
            const code = ev.data.code ?? 'error'
            const msg = ev.data.message ?? ''
            const content =
              code === 'secure_route_denied'
                ? `The secure inference route refused this request: ${msg}`
                : code === 'stream_interrupted'
                  ? `The answer stream was interrupted${msg ? `: ${msg}` : ''}.`
                  : `Ask failed (${code})${msg ? `: ${msg}` : ''}`
            patch((m) => ({ ...m, content: m.content ? `${m.content}\n\n${content}` : content }))
          }
        },
        abort.signal,
      )
      if (!finished) {
        queue.end()
        patch((m) => ({
          ...m,
          content: m.content || 'The answer stream ended before any answer arrived.',
        }))
      }
    } catch (error) {
      if (abort.signal.aborted) return
      queue.stop()
      const content = describeAskError(error)
      patch((m) => ({ ...m, content: m.content ? `${m.content}\n\n${content}` : content }))
    } finally {
      if (abortRef.current === abort) setLoading(false)
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)] space-y-6">
      <h1 className="text-3xl font-bold text-primary">
        Ask NerveCenter
      </h1>

      <div className="flex-1 bg-elevated rounded-lg shadow-lg overflow-hidden flex flex-col">
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {messages.map((message) => (
            <div
              key={message.id}
              className={`flex ${
                message.role === 'user' ? 'justify-end' : 'justify-start'
              }`}
            >
              <div
                className={`max-w-[85%] p-4 rounded-lg shadow ${
                  message.role === 'user'
                    ? 'bg-blue-500 text-white'
                    : 'bg-hover text-primary'
                }`}
              >
                <p className="text-sm whitespace-pre-wrap">{message.content}</p>
                {message.proposal && <CalendarProposalCard proposal={message.proposal} />}
                {message.undoEventId && <CreatedEventUndo eventId={message.undoEventId} />}
                <p className="text-xs mt-2 opacity-70">
                  {new Date(message.timestamp).toLocaleTimeString()}
                </p>

                {/* Provenance section */}
                {message.provenance && message.provenance.facts.length > 0 && (
                  <div className={`mt-3 pt-3 border-t ${message.role === 'user' ? 'border-blue-300' : 'border-gray-400'}`}>
                    <div className="text-xs font-semibold mb-2">Provenance</div>
                    <div className="space-y-2">
                      {message.provenance.facts.map((fact, idx) => (
                        <div key={idx} className="text-xs">
                          <span className="text-green-600 dark:text-green-400 font-semibold">✓ FACT</span>{' '}
                          <span className="opacity-80">"{fact.statement}"</span>
                          <div className="text-xs opacity-70 mt-0.5">
                            Confidence: {fact.confidence * 100}% | Evidence: {fact.evidence.join(', ')}
                          </div>
                        </div>
                      ))}
                      {message.provenance.hypotheses.map((hypo, idx) => (
                        <div key={idx} className="text-xs">
                          <span className="text-yellow-600 dark:text-yellow-400 font-semibold">❓ HYPOTHESIS</span>{' '}
                          <span className="opacity-80">"{hypo.statement}"</span>
                          <div className="text-xs opacity-70 mt-0.5">
                            Confidence: {hypo.confidence * 100}% | Evidence: {hypo.evidence.join(', ')}
                          </div>
                        </div>
                      ))}
                      {message.provenance.reasoning && (
                        <div className="text-xs opacity-70 mt-2 pt-2 border-t border-gray-300 dark:border-gray-600">
                          <span className="font-semibold">Reasoning:</span> {message.provenance.reasoning}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
        <div className="border-t border-line">
          {/* Optional per-request control (INFER_CONTROL_CONTRACT.md). Collapsed
              by default — the service default applies unless a model/provider or
              agent is chosen. Options load lazily on expand. */}
          <div className="px-4 pt-3">
            <button
              type="button"
              data-testid="ask-advanced-toggle"
              aria-expanded={pickerOpen}
              aria-controls="ask-advanced-panel"
              onClick={togglePicker}
              className="text-xs font-medium text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
            >
              {pickerOpen ? '▲ Hide' : '▼ Advanced'}
              {' '}model / agent override
            </button>

            {pickerOpen && (
              <div
                id="ask-advanced-panel"
                data-testid="ask-advanced-panel"
                className="mt-3 space-y-2"
              >
                {pickerError && (
                  <p
                    data-testid="ask-advanced-error"
                    role="alert"
                    className="text-xs text-red-600 dark:text-red-400"
                  >
                    {pickerError}
                  </p>
                )}
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                  <div>
                    <label
                      htmlFor="ask-agent-select"
                      className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1"
                    >
                      Agent
                    </label>
                    <select
                      id="ask-agent-select"
                      data-testid="ask-agent-select"
                      value={askAgent}
                      onChange={(e) => setAskAgent(e.target.value)}
                      disabled={pickerLoading}
                      className="w-full px-2 py-1.5 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-xs text-primary"
                    >
                      <option value="">Default (no agent)</option>
                      {pickerAgents.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label
                      htmlFor="ask-model-select"
                      className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1"
                    >
                      Model
                    </label>
                    <select
                      id="ask-model-select"
                      data-testid="ask-model-select"
                      value={askModel}
                      onChange={(e) => {
                        const v = e.target.value
                        setAskModel(v)
                        update({ askModel: v })
                      }}
                      disabled={pickerLoading}
                      className="w-full px-2 py-1.5 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-xs text-primary"
                    >
                      <option value="">
                        Default
                        {pickerAvailable
                          ? ` (${pickerAvailable.default.model})`
                          : ''}
                      </option>
                      {(pickerAvailable?.models ?? []).map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label
                      htmlFor="ask-provider-select"
                      className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1"
                    >
                      Provider
                    </label>
                    <select
                      id="ask-provider-select"
                      data-testid="ask-provider-select"
                      value={askProvider}
                      onChange={(e) => {
                        const v = e.target.value
                        setAskProvider(v)
                        update({ askProvider: v })
                      }}
                      disabled={pickerLoading}
                      className="w-full px-2 py-1.5 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-xs text-primary"
                    >
                      <option value="">
                        Default
                        {pickerAvailable
                          ? ` (${pickerAvailable.default.provider})`
                          : ''}
                      </option>
                      {(pickerAvailable?.providers ?? []).map((p) => (
                        <option key={p.provider} value={p.provider}>
                          {p.provider}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="px-4 pb-4">
          {(recorder.error || speechError) && (
            <p
              role="alert"
              data-testid="ask-speech-error"
              className="mb-2 text-xs text-amber-700 dark:text-amber-300"
            >
              {recorder.error ?? speechError}
            </p>
          )}
          <div className="flex space-x-2">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyPress={(e) => {
                if (e.key === 'Enter') void handleSendMessage()
              }}
              placeholder="Type your message..."
              className="flex-1 px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-white"
              disabled={loading}
            />
            <button
              type="button"
              data-testid="ask-mic"
              onClick={() => void handleMic()}
              disabled={loading || recorder.state === 'transcribing'}
              aria-label={recorder.state === 'recording' ? 'Stop recording and send' : 'Dictate a question'}
              title={isRecordingSupported() ? 'Dictate a question' : 'This browser cannot record audio'}
              className={`px-3 py-2 rounded-lg border border-line transition-colors disabled:opacity-50 ${
                recorder.state === 'recording' ? 'bg-red-500 text-white' : 'bg-hover text-primary'
              }`}
            >
              {recorder.state === 'recording' ? '■ Stop' : recorder.state === 'transcribing' ? '…' : '🎤'}
            </button>
            <button
              type="button"
              data-testid="ask-speak-toggle"
              aria-pressed={spoken}
              onClick={toggleSpoken}
              title="Speak replies aloud"
              className={`px-3 py-2 rounded-lg border border-line transition-colors ${
                spoken ? 'bg-blue-500 text-white' : 'bg-hover text-primary'
              }`}
            >
              {spoken ? '🔊' : '🔇'}
              <span className="sr-only">Spoken replies</span>
            </button>
            <button
              onClick={() => void handleSendMessage()}
              disabled={loading || !input.trim()}
              className="bg-blue-500 hover:bg-blue-600 text-white px-6 py-2 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? '...' : 'Send'}
            </button>
          </div>
          </div>
        </div>
      </div>

      <div className="bg-elevated rounded-lg shadow-lg p-6">
        <h3 className="text-lg font-semibold text-primary mb-4">
          Quick Questions
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {[
            'What are my active goals?',
            'Show me recent incidents',
            'What agents are running?',
            'List pending tasks',
          ].map((question, index) => (
            <button
              key={index}
              onClick={() => setInput(question)}
              className="text-left px-4 py-3 bg-hover rounded-lg hover:bg-gray-100 dark:hover:bg-gray-600 transition-colors"
            >
              <p className="text-sm text-gray-700 dark:text-gray-300">
                {question}
              </p>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

export { AskView }
