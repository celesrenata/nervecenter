import { expect, test } from '@playwright/test'

/**
 * AC005 — Ask round-trip against the REAL backend.
 *
 * Flow: seed auth → /ask → submit a question through the form →
 * the UI POSTs /api/context/ask, and the T003-04 grounded pipeline
 * answers HTTP 200 with the structured R107 policy denial: the
 * dependency-free deployment has no ALLOW inference policy, so the PDP
 * default-denies remote inference before any transport hop —
 * `{"accepted":true,"executed":false,"service":"context",
 * "status":"policy_denied","request_status":"denied","answer":…}` →
 * AskView renders the refusal `answer` string as a chat message.
 *
 * Asserts both the wire response (status 200 + policy-denial fields)
 * and exactly what the view renders from that payload, with zero console
 * errors. Same console-error policy as views.spec.ts (the only filter is the
 * `/api/decisions` dead-end 501 — masked via `msg.location().url`, since the
 * console text carries no URL — which does not fire on this route).
 *
 * Note: `/api/systems` is intentionally absent from this list — the endpoint
 * is implemented (wired at startup, returns 200 with live status rows) and no
 * longer produces a masked resource error on the /ask route.
 */

const KNOWN_MISSING_BACKEND_PATHS = ['/api/decisions']

test('Ask round-trip renders the /api/context/ask response without console errors', async ({
  page,
}) => {
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  const failedRequests: string[] = []
  const onConsole = (msg: import('@playwright/test').ConsoleMessage) => {
    if (msg.type() !== 'error') return
    const text = msg.text()
    if (text.startsWith('Failed to load resource:')) {
      // The URL is NOT in the console text — it lives on msg.location().url.
      const url = msg.location()?.url ?? ''
      if (KNOWN_MISSING_BACKEND_PATHS.some((p) => url.endsWith(p))) return
    }
    consoleErrors.push(text)
  }
  page.on('console', onConsole)
  page.on('pageerror', (err) => pageErrors.push(err.message))
  page.on('requestfailed', (req) =>
    failedRequests.push(`${req.method()} ${req.url()}`),
  )

  try {
    await page.addInitScript(() => {
      localStorage.setItem('authToken', 'e2e-token')
    })
    await page.goto('/ask', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Ask NerveCenter')

    // The greeting message is part of the initial render.
    await expect(page.getByText('Hello! I am NerveCenter. How can I help you today?')).toBeVisible()

    const question = 'What are my active goals?'
    const input = page.getByPlaceholder('Type your message...')
    await input.fill(question)

    const responsePromise = page.waitForResponse(
      (res) =>
        res.url().endsWith('/api/context/ask') && res.request().method() === 'POST',
    )
    await page.getByRole('button', { name: 'Send' }).click()
    const response = await responsePromise

    // Wire assertions on the real backend's T003-04 response. The
    // dependency-free deployment's default-deny R107 PDP pre-flight
    // rejects remote inference, so the endpoint answers a 200 with a
    // structured policy denial — no fabricated answer, no 202 ack.
    expect(response.status()).toBe(200)
    const body = await response.json()
    expect(body.accepted).toBe(true)
    expect(body.executed).toBe(false)
    expect(body.service).toBe('context')
    expect(typeof body.request_id).toBe('string')
    expect(typeof body.timestamp).toBe('string')
    expect(body.status).toBe('policy_denied')
    expect(body.request_status).toBe('denied')
    expect(body.policy?.denied).toBe(true)
    expect(typeof body.answer).toBe('string')
    expect(body.answer.length).toBeGreaterThan(0)

    // The user's question appears in the chat. Scope to the chat-bubble
    // paragraph: the same string also lives in the permanent "Quick
    // Questions" suggestion button below the chat, so a bare getByText
    // would hit two elements (strict-mode violation).
    const userBubble = page.locator('p.whitespace-pre-wrap', { hasText: question })
    await expect(userBubble.first()).toBeVisible()

    // What AskView actually renders from the policy-denial payload: the
    // refusal `answer` string as the assistant message, with no
    // provenance block (a denial carries no facts/hypotheses).
    const assistantText = page
      .locator('p')
      .filter({ hasText: 'This question cannot be answered here' })
    await expect(assistantText).toBeVisible()
    await expect(assistantText).toHaveText(body.answer)

    // No Provenance section for an ack payload.
    await expect(page.getByText('Provenance')).toHaveCount(0)

    await page.waitForTimeout(500)
    expect(pageErrors, 'uncaught exceptions').toEqual([])
    expect(failedRequests, 'failed network requests').toEqual([])
    expect(consoleErrors, 'console errors').toEqual([])
  } finally {
    page.off('console', onConsole)
  }
})
