import { expect, test, type Page } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * Ask sessions: tabs, and conversations (including an answer still streaming)
 * that survive leaving the Ask page. The ask stream is a fetch override whose
 * chunks the test pushes by hand, one controller per request.
 */

async function installStreamFake(page: Page) {
  await page.addInitScript(() => {
    const w = window as unknown as Record<string, any>
    const origFetch = window.fetch.bind(window)
    w.__askBodies = []
    w.__askCtls = []
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.includes('/api/context/ask/stream')) {
        const enc = new TextEncoder()
        let ctrl!: ReadableStreamDefaultController<Uint8Array>
        const stream = new ReadableStream<Uint8Array>({ start: (c) => (ctrl = c) })
        w.__askBodies.push(String(init?.body ?? ''))
        w.__askCtls.push({
          send: (ev: string, data: unknown) =>
            ctrl.enqueue(enc.encode(`event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`)),
          close: () => ctrl.close(),
        })
        return Promise.resolve(
          new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
        )
      }
      return origFetch(input, init)
    }
  })
  await page.route('**/api/voice/settings', (route) =>
    route.fulfill({ json: { tts_voice: 'af_sarah', spoken_replies: false } }),
  )
  await page.route('**/api/voice/sessions/*/mute', (route) => route.fulfill({ json: { ok: true } }))
}

const push = (page: Page, i: number, ev: string, data: unknown) =>
  page.evaluate(([i, ev, data]) => (window as any).__askCtls[i as number].send(ev, data), [i, ev, data] as const)
const closeStream = (page: Page, i: number) =>
  page.evaluate((i) => (window as any).__askCtls[i].close(), i)
const streams = (page: Page) => page.evaluate(() => (window as any).__askBodies.length as number)

async function ask(page: Page, question: string) {
  await page.getByPlaceholder('Type your message...').fill(question)
  await page.getByRole('button', { name: 'Send' }).click()
}

/** Client-side navigation (no reload), like clicking a sidebar link. */
async function navigate(page: Page, path: string) {
  await page.evaluate((p) => {
    window.history.pushState({}, '', p)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, path)
}

const bubble = (page: Page, text: string) => page.locator('p.whitespace-pre-wrap', { hasText: text })

test('an answer keeps streaming while away from Ask and is there on return', async ({ page }) => {
  await installStreamFake(page)
  await seedBreakGlassSession(page)
  await page.goto('/ask', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Ask NerveCenter')

  await ask(page, 'what is on my calendar')
  await expect.poll(() => streams(page)).toBe(1)
  await push(page, 0, 'delta', { text: 'You have ' })
  await expect(bubble(page, 'You have')).toBeVisible()

  await navigate(page, '/goals')
  await expect(page.locator('main h1')).not.toHaveText('Ask NerveCenter')
  // The stream finishes while the Ask page is not mounted.
  await push(page, 0, 'delta', { text: 'two meetings.' })
  await push(page, 0, 'done', { answer: 'You have two meetings.', status: 'answered' })
  await closeStream(page, 0)

  await navigate(page, '/ask')
  await expect(page.locator('main h1')).toHaveText('Ask NerveCenter')
  await expect(bubble(page, 'what is on my calendar').first()).toBeVisible()
  await expect(bubble(page, 'You have two meetings.')).toBeVisible()
  await expect(page.getByTestId('ask-tab-0')).toHaveText('what is on my calendar')
  await expect(page.getByTestId('ask-tab-0-busy')).toHaveCount(0)
})

test('tabs hold separate conversations that stream in parallel and survive a reload', async ({ page }) => {
  await installStreamFake(page)
  await seedBreakGlassSession(page)
  await page.goto('/ask', { waitUntil: 'domcontentloaded' })

  await ask(page, 'first question')
  await expect.poll(() => streams(page)).toBe(1)
  await expect(page.getByTestId('ask-tab-0-busy')).toBeVisible()

  await page.getByTestId('ask-tab-new').click()
  await expect(page.getByTestId('ask-tab-1')).toHaveAttribute('aria-selected', 'true')
  await expect(bubble(page, 'first question')).toHaveCount(0)
  // The first tab is still answering; the new one can ask on its own.
  await ask(page, 'second question')
  await expect.poll(() => streams(page)).toBe(2)
  const sessionIds = await page.evaluate(() =>
    (window as any).__askBodies.map((b: string) => JSON.parse(b).session_id),
  )
  expect(sessionIds[0]).not.toBe(sessionIds[1])

  await push(page, 1, 'done', { answer: 'Second answer.', status: 'answered' })
  await closeStream(page, 1)
  await push(page, 0, 'done', { answer: 'First answer.', status: 'answered' })
  await closeStream(page, 0)
  await expect(bubble(page, 'Second answer.')).toBeVisible()
  await expect(bubble(page, 'First answer.')).toHaveCount(0)

  await page.getByTestId('ask-tab-0').click()
  await expect(bubble(page, 'First answer.')).toBeVisible()
  await expect(bubble(page, 'Second answer.')).toHaveCount(0)

  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('ask-tab-0')).toHaveText('first question')
  await expect(page.getByTestId('ask-tab-1')).toHaveText('second question')
  await expect(bubble(page, 'First answer.')).toBeVisible()

  await page.getByTestId('ask-tab-0-close').click()
  await expect(page.getByTestId('ask-tab-0')).toHaveText('second question')
  await expect(page.getByTestId('ask-tab-1')).toHaveCount(0)
  await expect(bubble(page, 'Second answer.')).toBeVisible()
})
