import { useCallback, useEffect, useRef, useState } from 'react'

export type RecorderState = 'idle' | 'recording' | 'transcribing'

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']

export function isRecordingSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === 'function' &&
    typeof MediaRecorder !== 'undefined'
  )
}

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder.isTypeSupported !== 'function') return undefined
  return MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported(m))
}

/**
 * Microphone capture. `start()` asks for the mic and records; `stop()` resolves
 * with the recorded blob. `setState('transcribing')` lets the caller show the
 * upload phase in the same state machine.
 */
export function useVoiceRecorder() {
  const [state, setState] = useState<RecorderState>('idle')
  const [error, setError] = useState<string | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])

  const releaseStream = () => {
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
  }

  useEffect(
    () => () => {
      if (recorderRef.current && recorderRef.current.state !== 'inactive') {
        recorderRef.current.onstop = null
        recorderRef.current.stop()
      }
      releaseStream()
    },
    [],
  )

  const start = useCallback(async (): Promise<boolean> => {
    setError(null)
    if (!isRecordingSupported()) {
      setError('This browser cannot record audio. Type your question instead.')
      return false
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      const mimeType = pickMimeType()
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
      chunksRef.current = []
      recorder.ondataavailable = (e: BlobEvent) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data)
      }
      recorderRef.current = recorder
      recorder.start()
      setState('recording')
      return true
    } catch (err) {
      releaseStream()
      const name = err instanceof DOMException ? err.name : ''
      setError(
        name === 'NotAllowedError' || name === 'SecurityError'
          ? 'Microphone permission was denied. Allow microphone access for this site and try again.'
          : name === 'NotFoundError'
            ? 'No microphone was found on this device.'
            : 'Could not start recording the microphone.',
      )
      setState('idle')
      return false
    }
  }, [])

  const stop = useCallback((): Promise<Blob | null> => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state === 'inactive') {
      setState('idle')
      return Promise.resolve(null)
    }
    return new Promise((resolve) => {
      recorder.onstop = () => {
        const type = recorder.mimeType || chunksRef.current[0]?.type || 'audio/webm'
        const blob = chunksRef.current.length > 0 ? new Blob(chunksRef.current, { type }) : null
        chunksRef.current = []
        recorderRef.current = null
        releaseStream()
        setState('idle')
        resolve(blob)
      }
      recorder.stop()
    })
  }, [])

  return { state, setState, error, setError, start, stop }
}
