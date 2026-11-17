import { useEffect, useRef, useState } from 'react'
import { voiceApi, VoiceApiError, type VoiceOption, type VoiceSettings } from '../api/voice'

const SAMPLE = 'Hello, this is how I will sound when I read your answers.'

function describe(err: unknown): string {
  if (err instanceof VoiceApiError) {
    if (err.code === 'speech_unavailable') return 'The speech service is unavailable right now.'
    if (err.code === 'unknown_voice') return 'That voice is not available.'
    return `Voice request failed (${err.code}).`
  }
  return 'Could not reach the server.'
}

/** Settings → Voice: per-account TTS voice and the spoken-replies default. */
export function VoiceSettingsPanel() {
  const [voices, setVoices] = useState<VoiceOption[] | null>(null)
  const [settings, setSettings] = useState<VoiceSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    let cancelled = false
    Promise.all([voiceApi.getSettings(), voiceApi.getVoices()])
      .then(([s, v]) => {
        if (cancelled) return
        setSettings(s)
        setVoices(v.data)
      })
      .catch((e) => {
        if (!cancelled) setError(describe(e))
      })
    return () => {
      cancelled = true
      audioRef.current?.pause()
    }
  }, [])

  const save = async (patch: Partial<VoiceSettings>) => {
    setError(null)
    setSaved(false)
    const previous = settings
    setSettings((cur) => (cur ? { ...cur, ...patch } : cur)) // optimistic
    try {
      setSettings(await voiceApi.putSettings(patch))
      setSaved(true)
    } catch (e) {
      setSettings(previous)
      setError(describe(e))
    }
  }

  const preview = async () => {
    if (!settings) return
    setError(null)
    setPreviewing(true)
    try {
      const res = await voiceApi.speak({
        text: SAMPLE,
        voice: settings.tts_voice,
        session_id: `preview-${Date.now()}`,
      })
      if (!(res instanceof Blob)) {
        setError('Preview is muted for this session.')
        return
      }
      const audio = audioRef.current ?? new Audio()
      audioRef.current = audio
      audio.src = URL.createObjectURL(res)
      await audio.play()
    } catch (e) {
      setError(describe(e))
    } finally {
      setPreviewing(false)
    }
  }

  return (
    <section
      id="settings-panel-voice"
      role="tabpanel"
      aria-labelledby="settings-voice"
      className="bg-elevated rounded-lg shadow-lg p-6 space-y-6"
    >
      <div>
        <h2 id="settings-voice" className="text-xl font-semibold text-primary">
          Voice
        </h2>
        <p className="text-sm text-muted mt-1">
          How Ask talks back. These settings are saved to your account on the server.
        </p>
      </div>

      {error && (
        <p role="alert" data-testid="settings-voice-error" className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}

      {settings && voices ? (
        <div className="space-y-6">
          <div>
            <label htmlFor="settings-voice-select" className="block text-sm font-medium text-primary">
              TTS voice
            </label>
            <div className="mt-2 flex items-center gap-2">
              <select
                id="settings-voice-select"
                data-testid="settings-voice-select"
                value={settings.tts_voice}
                onChange={(e) => void save({ tts_voice: e.target.value })}
                className="w-full max-w-xs px-3 py-2 rounded-lg border border-line bg-hover/40 text-sm text-primary"
              >
                {voices.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                data-testid="settings-voice-preview"
                onClick={() => void preview()}
                disabled={previewing}
                className="text-sm border border-line rounded-lg px-3 py-2 hover:bg-hover disabled:opacity-50"
              >
                {previewing ? 'Playing…' : 'Preview'}
              </button>
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm text-primary">
            <input
              type="checkbox"
              data-testid="settings-voice-spoken"
              checked={settings.spoken_replies}
              onChange={(e) => void save({ spoken_replies: e.target.checked })}
            />
            Speak Ask replies by default
          </label>
          {saved && (
            <p data-testid="settings-voice-saved" className="text-xs text-muted">
              Saved.
            </p>
          )}
        </div>
      ) : (
        !error && <p className="text-sm text-muted">Loading voices…</p>
      )}
    </section>
  )
}
