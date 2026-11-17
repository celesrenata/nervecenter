import { expect, test, type Page } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * Voice + calendar Ask (FEAT-003).
 *
 * All voice, ask-stream and calendar-proposal traffic is mocked: the stream is
 * a fetch override whose chunks the test pushes by hand (so "mid-stream" is
 * real), the mic is a fake getUserMedia + MediaRecorder, and audio playback is
 * a fake HTMLMediaElement.play that records the call and fires `ended`.
 * Nothing here can create a real calendar event.
 */

const GENERIC = 'Sorry, I encountered an error processing your request.'

interface VoiceMock {
  speaks: Array<{ text: string; voice?: string; session_id: string }>
  mutes: Array<{ session_id: string; muted: boolean }>
  settingsPuts: Array<Record<string, unknown>>
  settings: { tts_voice: string; spoken_replies: boolean }
}

const VOICES = [
  { id: 'af_sarah', label: 'Sarah (US, female)', lang: 'en-US', gender: 'female', verified: true },
  { id: 'am_adam', label: 'Adam (US, male)', lang: 'en-US', gender: 'male', verified: true },
]

async function installBrowserFakes(page: Page) {
  await page.addInitScript(() => {
    const w = window as unknown as Record<string, any>
    // Controllable ask stream.
    const origFetch = window.fetch.bind(window)
    w.__askBodies = []
    w.__askCtl = null
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.includes('/api/context/ask/stream')) {
        const enc = new TextEncoder()
        let ctrl!: ReadableStreamDefaultController<Uint8Array>
        const stream = new ReadableStream<Uint8Array>({ start: (c) => (ctrl = c) })
        w.__askBodies.push(String(init?.body ?? ''))
        w.__askCtl = {
          send: (ev: string, data: unknown) =>
            ctrl.enqueue(enc.encode(`event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`)),
          close: () => ctrl.close(),
        }
        return Promise.resolve(
          new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
        )
      }
      return origFetch(input, init)
    }
    // Fake microphone.
    const md = navigator.mediaDevices as MediaDevices
    md.getUserMedia = async () => ({ getTracks: () => [{ stop() {} }] }) as unknown as MediaStream
    class FakeRecorder {
      static isTypeSupported(t: string) {
        return t === 'audio/webm;codecs=opus'
      }
      state = 'inactive'
      mimeType: string
      ondataavailable: ((e: { data: Blob }) => void) | null = null
      onstop: (() => void) | null = null
      constructor(_s: unknown, o?: { mimeType?: string }) {
        this.mimeType = o?.mimeType ?? 'audio/webm'
      }
      start() {
        this.state = 'recording'
      }
      stop() {
        this.state = 'inactive'
        this.ondataavailable?.({ data: new Blob(['voice'], { type: this.mimeType }) })
        this.onstop?.()
      }
    }
    ;(window as any).MediaRecorder = FakeRecorder
    // Fake playback.
    w.__plays = 0
    HTMLMediaElement.prototype.play = function (this: HTMLMediaElement) {
      w.__plays += 1
      const el = this
      return Promise.resolve().then(() => {
        queueMicrotask(() => el.dispatchEvent(new Event('ended')))
      })
    }
  })
}

async function mockVoice(page: Page, initial: Partial<VoiceMock['settings']> = {}): Promise<VoiceMock> {
  const mock: VoiceMock = {
    speaks: [],
    mutes: [],
    settingsPuts: [],
    settings: { tts_voice: 'af_sarah', spoken_replies: false, ...initial },
  }
  await page.route('**/api/voice/settings', async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as Partial<VoiceMock['settings']>
      mock.settingsPuts.push(body)
      mock.settings = { ...mock.settings, ...body }
    }
    await route.fulfill({ json: mock.settings })
  })
  await page.route('**/api/voice/voices', (route) =>
    route.fulfill({ json: { data: VOICES, default: 'af_sarah' } }),
  )
  await page.route('**/api/voice/sessions/*/mute', async (route) => {
    const m = /sessions\/([^/]+)\/mute/.exec(route.request().url())
    mock.mutes.push({
      session_id: decodeURIComponent(m?.[1] ?? ''),
      muted: (route.request().postDataJSON() as { muted: boolean }).muted,
    })
    await route.fulfill({ json: { ok: true } })
  })
  await page.route('**/api/voice/speak', async (route) => {
    mock.speaks.push(route.request().postDataJSON())
    await route.fulfill({ status: 200, contentType: 'audio/mpeg', body: Buffer.from('ID3fake') })
  })
  await page.route('**/api/voice/transcribe', (route) =>
    route.fulfill({ json: { text: 'what are my active goals', language: 'en', duration: 1.2 } }),
  )
  return mock
}

async function openAsk(page: Page) {
  await seedBreakGlassSession(page)
  await page.goto('/ask', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Ask NerveCenter')
}

const waitForStream = (page: Page, n = 1) =>
  expect
    .poll(() => page.evaluate(() => (window as any).__askBodies.length))
    .toBeGreaterThanOrEqual(n)

const send = (page: Page, ev: string, data: unknown) =>
  page.evaluate(([e, d]) => (window as any).__askCtl.send(e, d), [ev, data] as const)

const closeStream = (page: Page) => page.evaluate(() => (window as any).__askCtl.close())

const sendQuestion = async (page: Page, text: string) => {
  await page.getByPlaceholder('Type your message...').fill(text)
  await page.getByRole('button', { name: 'Send' }).click()
}

function collectErrors(page: Page) {
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()))
  page.on('pageerror', (e) => pageErrors.push(e.message))
  return { consoleErrors, pageErrors }
}

test('mic dictation shows the transcript, sends it, and streams the reply', async ({ page }) => {
  const errors = collectErrors(page)
  await installBrowserFakes(page)
  await mockVoice(page)
  await openAsk(page)

  await page.getByTestId('ask-mic').click() // start recording
  await page.getByTestId('ask-mic').click() // stop → transcribe → send
  await expect(page.locator('p.whitespace-pre-wrap', { hasText: 'what are my active goals' }).first()).toBeVisible()

  await waitForStream(page)
  const body = JSON.parse(await page.evaluate(() => (window as any).__askBodies[0]))
  expect(body.question).toBe('what are my active goals')

  await send(page, 'delta', { text: 'You have ' })
  await expect(page.getByText('You have', { exact: false }).first()).toBeVisible()
  await send(page, 'delta', { text: 'two goals.' })
  await send(page, 'done', { answer: 'You have two goals.', status: 'answered' })
  await closeStream(page)
  await expect(page.locator('p.whitespace-pre-wrap', { hasText: 'You have two goals.' })).toBeVisible()

  expect(errors.pageErrors).toEqual([])
  expect(errors.consoleErrors).toEqual([])
})

test('spoken replies on speaks per sentence; off sends mute=true and speaks nothing', async ({ page }) => {
  await installBrowserFakes(page)
  const voice = await mockVoice(page, { spoken_replies: true })
  await openAsk(page)
  const toggle = page.getByTestId('ask-speak-toggle')
  await expect(toggle).toHaveAttribute('aria-pressed', 'true')

  await sendQuestion(page, 'first question')
  await waitForStream(page)
  await send(page, 'delta', { text: 'First sentence. ' })
  await send(page, 'delta', { text: 'Second sentence.' })
  await send(page, 'done', { answer: 'First sentence. Second sentence.' })
  await closeStream(page)

  await expect.poll(() => voice.speaks.map((s) => s.text)).toEqual(['First sentence.', 'Second sentence.'])
  await expect.poll(() => page.evaluate(() => (window as any).__plays)).toBe(2)
  expect(new Set(voice.speaks.map((s) => s.session_id)).size).toBe(1)

  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-pressed', 'false')
  await expect.poll(() => voice.mutes.at(-1)).toMatchObject({ muted: true })
  expect(voice.settingsPuts.at(-1)).toEqual({ spoken_replies: false })

  const before = voice.speaks.length
  await sendQuestion(page, 'second question')
  await waitForStream(page, 2)
  await send(page, 'delta', { text: 'Silent answer. ' })
  await send(page, 'done', { answer: 'Silent answer.' })
  await closeStream(page)
  await expect(page.locator('p.whitespace-pre-wrap', { hasText: 'Silent answer.' })).toBeVisible()
  await page.waitForTimeout(300)
  expect(voice.speaks.length).toBe(before)
})

test('the TTS voice chosen in Settings is saved and used by Ask', async ({ page }) => {
  await installBrowserFakes(page)
  const voice = await mockVoice(page)
  await seedBreakGlassSession(page)
  await page.goto('/settings?tab=voice', { waitUntil: 'domcontentloaded' })

  await page.getByTestId('settings-voice-select').selectOption('am_adam')
  await expect.poll(() => voice.settingsPuts).toContainEqual({ tts_voice: 'am_adam' })
  await page.getByTestId('settings-voice-spoken').check()
  await expect.poll(() => voice.settingsPuts).toContainEqual({ spoken_replies: true })
  await page.getByTestId('settings-voice-preview').click()
  await expect.poll(() => voice.speaks.at(-1)?.voice).toBe('am_adam')
  voice.speaks.length = 0

  await page.goto('/ask', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('ask-speak-toggle')).toHaveAttribute('aria-pressed', 'true')
  await sendQuestion(page, 'hello there')
  await waitForStream(page)
  await send(page, 'delta', { text: 'Hi back.' })
  await send(page, 'done', { answer: 'Hi back.' })
  await closeStream(page)
  await expect.poll(() => voice.speaks.map((s) => s.voice)).toEqual(['am_adam'])
})

test('mute keeps requesting for 30 s then stops; unmute speaks only new text', async ({ page }) => {
  await installBrowserFakes(page)
  const voice = await mockVoice(page, { spoken_replies: true })
  await page.clock.install()
  await openAsk(page)
  await expect(page.getByTestId('ask-speak-toggle')).toHaveAttribute('aria-pressed', 'true')

  await sendQuestion(page, 'tell me a story')
  await waitForStream(page)
  await send(page, 'delta', { text: 'One. ' })
  await expect.poll(() => voice.speaks.length).toBe(1)

  await page.getByTestId('ask-speak-toggle').click() // mute
  await expect.poll(() => voice.mutes.at(-1)).toMatchObject({ muted: true })
  await send(page, 'delta', { text: 'Two. ' })
  await expect.poll(() => voice.speaks.length).toBe(2) // still inside the grace

  await page.clock.runFor(31_000)
  await send(page, 'delta', { text: 'Three. ' })
  await page.waitForTimeout(200)
  expect(voice.speaks.length).toBe(2) // past the grace: no more requests

  await page.getByTestId('ask-speak-toggle').click() // unmute mid-stream
  await expect.poll(() => voice.mutes.at(-1)).toMatchObject({ muted: false })
  await send(page, 'delta', { text: 'Four. ' })
  await expect.poll(() => voice.speaks.map((s) => s.text)).toEqual(['One.', 'Two.', 'Four.'])

  await send(page, 'done', { answer: 'One. Two. Three. Four.' })
  await closeStream(page)
})

test('calendar proposal: confirm with the chosen account, then the consent path', async ({ page }) => {
  await installBrowserFakes(page)
  await mockVoice(page)
  const proposal = (id: string) => ({
    id,
    title: 'Appointment with Michal Inspektor',
    start: '2030-06-04T16:30:00-07:00',
    end: '2030-06-04T17:30:00-07:00',
    tz: 'America/Los_Angeles',
    all_day: false,
    account: { id: 'acc-1', label: 'celes@frameshift.net', can_write: true },
    accounts: [
      { id: 'acc-1', label: 'celes@frameshift.net', can_write: true, connection_id: 'conn-1' },
      { id: 'acc-2', label: 'work@example.com', can_write: true, connection_id: 'conn-2' },
    ],
    warnings: [],
    assumptions: ['Read 4:30 as 4:30 PM'],
    conflicts: [],
    requires_confirmation: true,
  })
  const confirms: Array<{ id: string; body: Record<string, unknown> }> = []
  let mode: 'ok' | 'consent' = 'ok'
  await page.route('**/api/calendar/proposals/*/confirm', async (route) => {
    const id = /proposals\/([^/]+)\/confirm/.exec(route.request().url())?.[1] ?? ''
    confirms.push({ id, body: route.request().postDataJSON() })
    if (mode === 'consent') {
      await route.fulfill({
        status: 409,
        json: { code: 'consent_required', message: 'write scope missing', connection_id: 'conn-1' },
      })
      return
    }
    await route.fulfill({
      json: {
        status: 'created',
        proposal_id: id,
        event: { event_id: 'ev1', title: 'x', start: 'a', end: 'b' },
      },
    })
  })
  const authStarts: Array<Record<string, unknown>> = []
  await page.route('**/api/connections/conn-1/auth', async (route) => {
    authStarts.push(route.request().postDataJSON())
    await route.fulfill({
      json: { authorization_url: 'http://localhost:3000/__google-consent', state: 's', provider: 'google' },
    })
  })
  await page.route('**/__google-consent', (route) =>
    route.fulfill({ contentType: 'text/html', body: '<html><body>consent</body></html>' }),
  )

  await openAsk(page)
  const sentence =
    'hey! can you add my appointment with Michal Inspektor to my calendar for tomorrow, Tuesday, at 4:30?'
  await sendQuestion(page, sentence)
  await waitForStream(page)
  await send(page, 'proposal', proposal('p1'))
  await send(page, 'done', {
    status: 'proposal',
    answer: 'I can add Appointment with Michal Inspektor on Tue Jun 4 at 4:30 PM to celes@frameshift.net. Confirm?',
    proposal: proposal('p1'),
  })
  await closeStream(page)

  const card = page.getByTestId('calendar-proposal')
  await expect(card).toContainText('Michal Inspektor')
  await expect(card.getByTestId('cal-assumptions')).toContainText('4:30 PM')
  await expect(card.getByTestId('cal-account')).toHaveValue('acc-1')
  await card.getByTestId('cal-account').selectOption('acc-2')
  await card.getByTestId('cal-confirm').click()
  await expect(card.getByTestId('cal-result')).toHaveText('Added to work@example.com')
  expect(confirms).toEqual([{ id: 'p1', body: { account_id: 'acc-2' } }])

  // Consent path: a second request whose confirm is refused for write scope.
  mode = 'consent'
  await sendQuestion(page, sentence)
  await waitForStream(page, 2)
  await send(page, 'done', { status: 'proposal', answer: 'Confirm?', proposal: proposal('p2') })
  await send(page, 'proposal', proposal('p2'))
  await closeStream(page)
  const card2 = page.getByTestId('calendar-proposal').last()
  await card2.getByTestId('cal-confirm').click()
  await expect(card2.getByTestId('cal-consent')).toContainText(
    'Google needs permission to write to this calendar',
  )
  await card2.getByTestId('cal-grant').click()
  await expect.poll(() => authStarts).toEqual([{ extra: ['calendar_write'] }])
  await page.waitForURL('**/__google-consent')
})

test('confirm-first off: a created event offers Undo', async ({ page }) => {
  await installBrowserFakes(page)
  await mockVoice(page)
  const undos: string[] = []
  await page.route('**/api/calendar/events/*/undo', async (route) => {
    undos.push(/events\/([^/]+)\/undo/.exec(route.request().url())?.[1] ?? '')
    await route.fulfill({ json: { undone: true, event_id: 'nc123' } })
  })
  await openAsk(page)
  await sendQuestion(page, 'add lunch with Sam tomorrow at noon')
  await waitForStream(page)
  await send(page, 'done', {
    status: 'created',
    answer: 'Added Lunch with Sam on Wed at 12:00 PM to celes@frameshift.net.',
    event: { event_id: 'nc123', title: 'Lunch with Sam', start: 'a', end: 'b' },
    undo: { event_id: 'nc123', expires_at: '2030-01-01T00:00:00Z' },
  })
  await closeStream(page)
  await expect(page.getByText('Added Lunch with Sam', { exact: false })).toBeVisible()
  await expect(page.getByTestId('calendar-proposal')).toHaveCount(0)
  await page.getByTestId('cal-undo').click()
  await expect(page.getByTestId('cal-undone')).toHaveText('Removed from your calendar.')
  expect(undos).toEqual(['nc123'])
})

test('specific error messages for 401, network failure and unavailable inference', async ({ page }) => {
  await mockVoice(page)
  await page.route('**/api/auth/refresh', (route) => route.fulfill({ status: 500, json: { detail: 'down' } }))
  let mode: '401' | 'network' | 'unavailable' = '401'
  await page.route('**/api/context/ask/stream', async (route) => {
    if (mode === '401') {
      await route.fulfill({ status: 401, json: { detail: 'Not authenticated' } })
    } else if (mode === 'network') {
      await route.abort('connectionrefused')
    } else {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body:
          'event: done\ndata: ' +
          JSON.stringify({
            accepted: true,
            executed: false,
            status: 'unavailable',
            reason: 'transport_not_configured',
          }) +
          '\n\n',
      })
    }
  })
  await openAsk(page)

  await sendQuestion(page, 'one')
  await expect(page.getByText('Your session expired', { exact: false })).toBeVisible()

  mode = 'network'
  await sendQuestion(page, 'two')
  await expect(page.getByText('Could not reach the server', { exact: false })).toBeVisible()

  mode = 'unavailable'
  await sendQuestion(page, 'three')
  await expect(page.getByText('Inference unavailable: transport_not_configured', { exact: false })).toBeVisible()

  await expect(page.getByText(GENERIC)).toHaveCount(0)
})
