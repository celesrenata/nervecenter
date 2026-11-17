/**
 * Voice (speech) API client — `backend.api.endpoints.voice`.
 *
 *   GET  /api/voice/settings                     → { tts_voice, spoken_replies }
 *   PUT  /api/voice/settings                     → same shape (422 unknown_voice)
 *   GET  /api/voice/voices                       → { data: Voice[], default }
 *   PUT  /api/voice/sessions/{id}/mute           → mute registry (30 s grace)
 *   POST /api/voice/speak                        → audio bytes | 409 muted
 *   POST /api/voice/transcribe                   → { text, language, duration }
 *
 * Errors are `{detail:{code}}`; they surface as `VoiceApiError` with the stable
 * code so callers can show a specific message.
 */

import { apiFetch, useMockMode } from './client'

export interface VoiceSettings {
  tts_voice: string
  spoken_replies: boolean
}

export interface VoiceOption {
  id: string
  label: string
  lang: string
  gender: string
  verified: boolean
}

export interface VoiceList {
  data: VoiceOption[]
  default: string
}

export interface Transcript {
  text: string
  language: string
  duration: number
}

/** A speak request that the server refused because the session is muted. */
export interface SpeakMuted {
  muted: true
}

export class VoiceApiError extends Error {
  status: number
  code: string

  constructor(status: number, code: string, message?: string) {
    super(message ?? `Voice request failed: ${status} ${code}`)
    this.name = 'VoiceApiError'
    this.status = status
    this.code = code
  }
}

async function voiceErrorFromResponse(res: Response): Promise<VoiceApiError> {
  let code = `http_${res.status}`
  try {
    const body = (await res.json()) as { detail?: unknown }
    const detail = body.detail
    if (detail && typeof detail === 'object' && typeof (detail as { code?: unknown }).code === 'string') {
      code = (detail as { code: string }).code
    } else if (typeof detail === 'string' && detail.length > 0) {
      code = detail
    }
  } catch {
    /* non-JSON body — keep the status code */
  }
  return new VoiceApiError(res.status, code)
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

const MOCK_VOICES: VoiceList = {
  data: [{ id: 'af_sarah', label: 'Sarah (US, female)', lang: 'en-US', gender: 'female', verified: true }],
  default: 'af_sarah',
}

const realVoiceApi = {
  getSettings: async (): Promise<VoiceSettings> => {
    const res = await apiFetch('/api/voice/settings')
    if (!res.ok) throw await voiceErrorFromResponse(res)
    return (await res.json()) as VoiceSettings
  },

  putSettings: async (settings: Partial<VoiceSettings>): Promise<VoiceSettings> => {
    const res = await apiFetch('/api/voice/settings', {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(settings),
    })
    if (!res.ok) throw await voiceErrorFromResponse(res)
    return (await res.json()) as VoiceSettings
  },

  getVoices: async (): Promise<VoiceList> => {
    const res = await apiFetch('/api/voice/voices')
    if (!res.ok) throw await voiceErrorFromResponse(res)
    return (await res.json()) as VoiceList
  },

  transcribe: async (blob: Blob): Promise<Transcript> => {
    const res = await apiFetch('/api/voice/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': blob.type || 'audio/webm' },
      body: blob,
    })
    if (!res.ok) throw await voiceErrorFromResponse(res)
    return (await res.json()) as Transcript
  },

  speak: async (
    body: { text: string; voice?: string; session_id: string; format?: 'mp3' | 'wav' },
    signal?: AbortSignal,
  ): Promise<Blob | SpeakMuted> => {
    const res = await apiFetch('/api/voice/speak', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ format: 'mp3', ...body }),
      signal,
    })
    if (res.status === 409) return { muted: true }
    if (!res.ok) throw await voiceErrorFromResponse(res)
    return await res.blob()
  },

  setMuted: async (sessionId: string, muted: boolean): Promise<void> => {
    const res = await apiFetch(`/api/voice/sessions/${encodeURIComponent(sessionId)}/mute`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ muted }),
    })
    if (!res.ok) throw await voiceErrorFromResponse(res)
  },
}

const mockVoiceApi: typeof realVoiceApi = {
  getSettings: async () => ({ tts_voice: 'af_sarah', spoken_replies: false }),
  putSettings: async (s) => ({ tts_voice: 'af_sarah', spoken_replies: false, ...s }),
  getVoices: async () => MOCK_VOICES,
  transcribe: async () => {
    throw new VoiceApiError(503, 'speech_unavailable', 'Speech is not available in mock mode')
  },
  speak: async () => ({ muted: true }),
  setMuted: async () => undefined,
}

export const voiceApi = useMockMode ? mockVoiceApi : realVoiceApi
