import { useState, useEffect, useSyncExternalStore } from 'react'
import { voiceApi } from '../api/voice'
import { CalendarProposalCard, CreatedEventUndo } from '../components/CalendarProposalCard'
import { useVoiceRecorder, isRecordingSupported } from '../voice/useVoiceRecorder'
import { inferenceApi, type AvailableInference } from '../api/inference'
import { agentsApi, type Agent } from '../api/agents'
import { useSettings } from '../theme/SettingsContext'
import { askSessions, describeSpeechError, voicePrefs } from './ask/askSessions'

const AskView = () => {
  const { settings, update } = useSettings()
  // Sessions (tabs) live in a module store so they and any streaming answer
  // survive leaving this page.
  const store = useSyncExternalStore(askSessions.subscribe, askSessions.getState)
  const active = store.sessions.find((s) => s.id === store.activeId) ?? store.sessions[0]
  const messages = active.messages
  const input = active.input
  const loading = active.loading
  const speechError = active.speechError
  const setInput = (v: string) => askSessions.setInput(active.id, v)
  const setSpeechError = (v: string | null) => askSessions.setSpeechError(active.id, v)

  const [spoken, setSpoken] = useState(voicePrefs.spoken)
  const recorder = useVoiceRecorder()

  // INFER_CONTROL_CONTRACT.md (web scope): optional per-request inference +
  // agent control. The picker is COLLAPSED by default — "default" keeps the
  // service default — so the common path sends a bare `{question}` body.
  // Options are fetched lazily, only when the user expands the picker.
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
    void askSessions.hydrate()
  }, [])

  useEffect(() => {
    let cancelled = false
    voiceApi
      .getSettings()
      .then((v) => {
        if (cancelled) return
        voicePrefs.voice = v.tts_voice
        voicePrefs.spoken = v.spoken_replies
        setSpoken(v.spoken_replies)
      })
      .catch(() => {
        /* defaults stay: spoken replies off, server default voice */
      })
    // Leaving the page keeps sessions, streams and speech running.
    return () => {
      cancelled = true
    }
  }, [])

  const toggleSpoken = () => {
    const next = !voicePrefs.spoken
    setSpoken(next)
    setSpeechError(null)
    askSessions.setSpoken(next)
    void voiceApi.putSettings({ spoken_replies: next }).catch((e) => setSpeechError(describeSpeechError(e)))
  }

  const handleSendMessage = (override?: string) =>
    askSessions.send(active.id, override ?? input, {
      agent: askAgent || undefined,
      model: askModel || undefined,
      provider: askProvider || undefined,
    })

  const handleMic = async () => {
    if (recorder.state === 'transcribing' || loading) return
    if (recorder.state === 'idle') {
      await recorder.start()
      return
    }
    const sessionId = active.id
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
        askSessions.setSpeechError(sessionId, 'No speech was detected in the recording.')
        return
      }
      askSessions.setSpeechError(sessionId, null)
      void askSessions.send(sessionId, text, {
        agent: askAgent || undefined,
        model: askModel || undefined,
        provider: askProvider || undefined,
      })
    } catch (err) {
      recorder.setState('idle')
      askSessions.setSpeechError(sessionId, describeSpeechError(err))
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)] space-y-6">
      <h1 className="text-3xl font-bold text-primary">
        Ask NerveCenter
      </h1>

      <div className="flex-1 bg-elevated rounded-lg shadow-lg overflow-hidden flex flex-col">
        <div
          role="tablist"
          aria-label="Ask sessions"
          data-testid="ask-tabs"
          className="flex items-end gap-1 overflow-x-auto border-b border-line px-2 pt-2"
        >
          {store.sessions.map((s, i) => {
            const selected = s.id === active.id
            return (
              <div
                key={s.id}
                className={`group flex items-center rounded-t-md border border-b-0 text-sm ${
                  selected ? 'border-line bg-hover text-primary' : 'border-transparent text-secondary hover:bg-hover'
                }`}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  data-testid={`ask-tab-${i}`}
                  onClick={() => askSessions.activate(s.id)}
                  title={s.title}
                  className="flex max-w-[14rem] items-center gap-2 truncate px-3 py-1.5"
                >
                  {s.loading && (
                    <span
                      data-testid={`ask-tab-${i}-busy`}
                      aria-label="answering"
                      className="inline-block h-2 w-2 shrink-0 animate-pulse rounded-full bg-blue-500"
                    />
                  )}
                  <span className="truncate">{s.title}</span>
                </button>
                <button
                  type="button"
                  data-testid={`ask-tab-${i}-close`}
                  aria-label={`Close ${s.title}`}
                  onClick={() => askSessions.close(s.id)}
                  className="px-2 py-1.5 text-xs opacity-60 hover:opacity-100"
                >
                  ×
                </button>
              </div>
            )
          })}
          <button
            type="button"
            data-testid="ask-tab-new"
            aria-label="New chat"
            title="New chat"
            onClick={() => askSessions.create()}
            className="mb-0.5 rounded-md px-3 py-1 text-lg leading-none text-secondary hover:bg-hover hover:text-primary"
          >
            +
          </button>
        </div>
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
