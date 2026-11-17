/**
 * Incremental per-sentence text-to-speech playback for streamed Ask replies.
 *
 * Deltas are pushed in as they arrive; each completed sentence is fetched
 * from `/api/voice/speak` one at a time, with the next sentence prefetched
 * while the current one plays. Playback goes through a single audio element.
 *
 * Mute semantics (mirrors the server's 30 s grace):
 *  - muted: the queue keeps requesting audio and keeps playback position, but
 *    the audio element is silenced, so unmuting inside the grace resumes at
 *    the current point.
 *  - muted for longer than `graceMs`: the queue stops requesting, drops queued
 *    audio, cancels in-flight fetches and ignores further text.
 *  - unmute after that cut-off discards the text received so far; speech
 *    resumes with the first sentence that completes after the unmute.
 *  - a `{muted:true}` reply from the server cuts off the same way.
 */

import type { SpeakMuted } from '../api/voice'

export const MUTE_GRACE_MS = 30_000

export interface SpeechQueueOptions {
  speak: (text: string, signal: AbortSignal) => Promise<Blob | SpeakMuted>
  onError?: (error: unknown) => void
  graceMs?: number
  /** Start already cut off (spoken replies off); text is ignored until unmute. */
  startMuted?: boolean
}

interface Item {
  text: string
  abort: AbortController
  promise?: Promise<Blob | null>
}

const SENTENCE_END = /[.!?]+["')\]]*(?=\s)|\n+/

/** Split `buffer` into completed sentences and the unfinished remainder. */
export function splitSentences(buffer: string): { sentences: string[]; rest: string } {
  const sentences: string[] = []
  let rest = buffer
  for (;;) {
    const m = SENTENCE_END.exec(rest)
    if (!m) break
    const end = m.index + m[0].length
    const sentence = rest.slice(0, end).trim()
    rest = rest.slice(end)
    if (sentence) sentences.push(sentence)
  }
  return { sentences, rest }
}

export class SpeechQueue {
  private readonly speakFn: SpeechQueueOptions['speak']
  private readonly onError?: (error: unknown) => void
  private readonly graceMs: number
  private muted: boolean
  private cutoff: boolean
  private stopped = false
  private buffer = ''
  private items: Item[] = []
  private running = false
  private graceTimer: ReturnType<typeof setTimeout> | null = null
  private audio: HTMLAudioElement | null = null
  private objectUrl: string | null = null
  private cancelPlay: (() => void) | null = null

  constructor(opts: SpeechQueueOptions) {
    this.speakFn = opts.speak
    this.onError = opts.onError
    this.graceMs = opts.graceMs ?? MUTE_GRACE_MS
    this.muted = !!opts.startMuted
    this.cutoff = !!opts.startMuted
  }

  /** Feed a streamed text delta. */
  push(delta: string): void {
    if (this.stopped || this.cutoff || !delta) return
    this.buffer += delta
    const { sentences, rest } = splitSentences(this.buffer)
    this.buffer = rest
    for (const s of sentences) this.enqueue(s)
  }

  /** The stream finished: speak whatever unfinished sentence remains. */
  end(): void {
    if (this.stopped || this.cutoff) return
    const tail = this.buffer.trim()
    this.buffer = ''
    if (tail) this.enqueue(tail)
  }

  setMuted(muted: boolean): void {
    if (this.stopped || muted === this.muted) return
    this.muted = muted
    if (this.audio) this.audio.muted = muted
    if (muted) {
      this.graceTimer = setTimeout(() => this.cutOff(), this.graceMs)
      return
    }
    if (this.graceTimer) {
      clearTimeout(this.graceTimer)
      this.graceTimer = null
    }
    if (this.cutoff) {
      // Resume from the current point: forget everything received so far.
      this.cutoff = false
      this.buffer = ''
    }
  }

  /** Stop everything for good (new ask, leaving the page). */
  stop(): void {
    this.stopped = true
    if (this.graceTimer) clearTimeout(this.graceTimer)
    this.graceTimer = null
    this.dropAll()
    this.buffer = ''
    if (this.audio) {
      this.audio.removeAttribute('src')
      this.audio = null
    }
  }

  private cutOff(): void {
    this.graceTimer = null
    this.cutoff = true
    this.buffer = ''
    this.dropAll()
  }

  private dropAll(): void {
    for (const item of this.items) item.abort.abort()
    this.items = []
    if (this.audio) this.audio.pause()
    this.cancelPlay?.()
    this.revoke()
  }

  private revoke(): void {
    if (this.objectUrl) {
      URL.revokeObjectURL(this.objectUrl)
      this.objectUrl = null
    }
  }

  private enqueue(text: string): void {
    this.items.push({ text, abort: new AbortController() })
    this.prefetch()
    void this.run()
  }

  /** Keep the playing sentence and the next one fetched. */
  private prefetch(): void {
    for (const item of this.items.slice(0, 2)) {
      if (item.promise) continue
      item.promise = this.speakFn(item.text, item.abort.signal).then(
        (res) => {
          if (res instanceof Blob) return res
          // Server says the session is muted past the grace: stop requesting.
          this.cutOff()
          return null
        },
        (err) => {
          if (!item.abort.signal.aborted) {
            this.onError?.(err)
            // A failed speech request ends this reply's speech; text is unaffected.
            this.cutOff()
          }
          return null
        },
      )
    }
  }

  private async run(): Promise<void> {
    if (this.running) return
    this.running = true
    try {
      while (this.items.length > 0 && !this.stopped) {
        const current = this.items[0]
        this.prefetch()
        const blob = await current.promise
        if (this.items[0] !== current) continue // dropped while fetching
        if (!blob) {
          this.items.shift()
          continue
        }
        await this.play(blob)
        if (this.items[0] === current) this.items.shift()
        this.prefetch()
      }
    } finally {
      this.running = false
    }
  }

  private play(blob: Blob): Promise<void> {
    return new Promise<void>((resolve) => {
      if (this.stopped) return resolve()
      const audio = this.audio ?? new Audio()
      this.audio = audio
      this.revoke()
      this.objectUrl = URL.createObjectURL(blob)
      audio.src = this.objectUrl
      audio.muted = this.muted
      const finish = () => {
        audio.removeEventListener('ended', finish)
        audio.removeEventListener('error', finish)
        this.cancelPlay = null
        resolve()
      }
      this.cancelPlay = finish
      audio.addEventListener('ended', finish)
      audio.addEventListener('error', finish)
      audio.play().catch(finish)
    })
  }
}
