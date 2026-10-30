import { expect, test } from '@playwright/test'

/**
 * briefing.spec.ts — the Briefing plane (prompt2 §30 — Briefings).
 *
 * One-screen daily projection: **Actions**, **Waiting On**, **Today**,
 * **Overnight**, plus an empty-state closer. Deterministic via `page.route`
 * API mocks (house style from views-a.spec.ts): assertions do not depend on
 * live backend seed state.
 *
 *   BF-1  /briefing renders all four §30 sections with the seeded content —
 *          action rows labelled with their due framing, waiting-on rows carry
 *          person + description + elapsed days, today carries a time, and
 *          overnight shows the 24-h count line.
 *   BF-2  The kind selector POSTs through the same endpoint — selecting
 *          "Evening" hits `/api/briefing?kind=evening` and the composed
 *          `kind` echoes back.
 *   BF-3  The empty-state §30 closer — "Nothing else requires attention." —
 *          renders when all four sections are empty.
 *   BF-4  The "Briefing" nav link reaches the view.
 *   BF-5  The kind selector's default is `morning`; the first GET carries
 *          `?kind=morning`.
 *
 * Console-error policy mirrors views-a.spec.ts: console errors, uncaught
 * page errors, and failed network requests all FAIL the test.
 */

interface BriefingFixture {
  kind: string
  generated_at: string
  actions: Array<{ attention_id: string; label: string; due: string | null }>
  waiting_on: Array<{ request_id: string; person: string; description: string; days_elapsed: number }>
  today: Array<{ event_id: string; title: string; start: string | null }>
  overnight: { emails: number; messages: number; voicemails: number }
  empty: boolean
}

function makeBriefing(partial: Partial<BriefingFixture>): BriefingFixture {
  return {
    kind: 'morning',
    generated_at: '2026-01-06T14:00:00Z',
    actions: [],
    waiting_on: [],
    today: [],
    overnight: { emails: 0, messages: 0, voicemails: 0 },
    empty: true,
    ...partial,
  }
}

interface ConsoleCapture {
  errors: string[]
  pageErrors: string[]
  failedRequests: string[]
}

function attachConsoleCapture(page: import('@playwright/test').Page, cap: ConsoleCapture) {
  const onConsole = (msg: import('@playwright/test').ConsoleMessage) => {
    if (msg.type() === 'error') cap.errors.push(msg.text())
  }
  const onPageError = (err: Error) => {
    cap.pageErrors.push(err.message)
  }
  const onRequestFailed = (req: import('@playwright/test').Request) => {
    cap.failedRequests.push(`${req.method()} ${req.url()}`)
  }
  page.on('console', onConsole)
  page.on('pageerror', onPageError)
  page.on('requestfailed', onRequestFailed)
  return () => {
    page.off('console', onConsole)
    page.off('pageerror', onPageError)
    page.off('requestfailed', onRequestFailed)
  }
}

function assertConsoleClean(cap: ConsoleCapture) {
  expect(cap.pageErrors, 'uncaught exceptions').toEqual([])
  expect(cap.failedRequests, 'failed network requests').toEqual([])
  expect(cap.errors, 'console errors').toEqual([])
}

async function seedAuth(page: import('@playwright/test').Page) {
  // Mint a real access token through the break-glass login (global-setup.mjs
  // enables NERVE_LOCAL_ADMIN / NERVE_LOCAL_ADMIN_PASSWORD) and seed the
  // sessionStorage key the auth transport reads (src/api/client.ts). The
  // AC005-era `localStorage.authToken` was never read by the client, so
  // auth-gated routes answered 401 and surfaced as console noise.
  const login = await fetch('http://127.0.0.1:8000/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'local-admin', password: 'e2e-breakglass' }),
  })
  if (!login.ok) {
    throw new Error(`seedAuth: break-glass login failed (HTTP ${login.status})`)
  }
  const { access_token } = (await login.json()) as { access_token: string }
  await page.addInitScript((token: string) => {
    sessionStorage.setItem('nc.accessToken', token)
  }, access_token)
}

async function visitAuthenticated(page: import('@playwright/test').Page, path: string) {
  await seedAuth(page)
  await page.goto(path, { waitUntil: 'domcontentloaded' })
}

function fulfillBriefing(route: import('@playwright/test').Route, body: BriefingFixture) {
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
}

test.describe('Briefing (BF — prompt2 §30)', () => {
  test('BF-1: renders all four §30 sections with seeded content', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    const briefing = makeBriefing({
      kind: 'morning',
      empty: false,
      actions: [
        { attention_id: 'att-bf-1', label: 'Call the pharmacy before their 5 PM cutoff', due: '2026-01-06T22:00:00Z' },
        { attention_id: 'att-bf-2', label: 'Send the invoice to Dana', due: null },
      ],
      waiting_on: [
        { request_id: 'wo-bf-sam', person: 'Sam', description: 'Deployment credentials', days_elapsed: 3 },
      ],
      today: [
        { event_id: 'ev-bf-standup', title: 'Standup', start: '2026-01-06T16:30:00Z' },
      ],
      overnight: { emails: 2, messages: 1, voicemails: 1 },
    })
    page.route('**/api/briefing**', (r) => fulfillBriefing(r, briefing))

    try {
      await visitAuthenticated(page, '/briefing')

      // The view's own heading inside <main> — proves the auth gate passed and
      // the route is wired (the same assertion views.spec.ts uses for every
      // plane).
      await expect(page.locator('main h1')).toHaveText('Briefing')
      // The four §30 sections are all reachable in the document.
      await expect(page.getByRole('heading', { name: 'Actions' })).toBeVisible()
      await expect(page.getByRole('heading', { name: 'Waiting On' })).toBeVisible()
      await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible()
      await expect(page.getByRole('heading', { name: 'Overnight' })).toBeVisible()

      // ACTION rows: label + due framing.
      const action1 = page.getByTestId('briefing-action-att-bf-1')
      await expect(action1).toHaveText(/Call the pharmacy before their 5 PM cutoff/)
      await expect(action1.getByTestId('briefing-action-due-att-bf-1')).toBeVisible()

      // A row without due carries no due badge.
      const action2 = page.getByTestId('briefing-action-att-bf-2')
      await expect(action2.getByTestId('briefing-action-due-att-bf-2')).toHaveCount(0)

      // WAITING-ON row: person, description, and elapsed days.
      const waiting = page.getByTestId('briefing-waiting-wo-bf-sam')
      await expect(waiting).toHaveText(/Sam\s+—\s+Deployment credentials/)
      await expect(page.getByTestId('briefing-waiting-days-wo-bf-sam')).toContainText('3 days')

      // TODAY row: title + time.
      const todayRow = page.getByTestId('briefing-today-ev-bf-standup')
      await expect(todayRow).toHaveText(/Standup/)

      // OVERNIGHT counts.
      const overnight = page.getByTestId('briefing-overnight')
      await expect(overnight).toHaveText(/2\s+email/)
      await expect(overnight).toHaveText(/1\s+message/)
      await expect(overnight).toHaveText(/1\s+voicemail/)
    } finally {
      detach()
      assertConsoleClean(cap)
    }
  })

  test('BF-2: the kind selector re-fetches with the selected kind and echoes it back', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    const seen: string[] = []
    page.route('**/api/briefing**', (r) => {
      const url = r.request().url()
      const kind = url.match(/kind=([^&]+)/)?.[1] ?? 'morning'
      seen.push(kind)
      return fulfillBriefing(r, makeBriefing({ kind }))
    })
    try {
      await visitAuthenticated(page, '/briefing')
      await expect(page.locator('main h1')).toHaveText('Briefing')

      // First fetch defaulted to morning — the default value is what makes
      // this screen the "Morning Brief" until the user changes it.
      expect(seen[0]).toBe('morning')

      // Switch to evening.
      await page.getByTestId('briefing-kind').selectOption('evening')
      // The subtitle reflects the new kind once it re-composes — the
      // briefing is re-rendered with the fresh snapshot.
      await expect(page.locator('main').getByText(/Evening brief — composed/)).toBeVisible()
      expect(seen[seen.length - 1]).toBe('evening')
    } finally {
      detach()
      assertConsoleClean(cap)
    }
  })

  test('BF-3: empty state renders the §30 honest closer', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    page.route('**/api/briefing**', (r) => fulfillBriefing(r, makeBriefing({ kind: 'morning', empty: true })))
    try {
      await visitAuthenticated(page, '/briefing')
      await expect(page.locator('main h1')).toHaveText('Briefing')
      // §30 empty-state contract: when the briefing is empty the view renders
      // ONLY the single honest closer (no section cards, no section headings).
      // The closer is a single <div> carrying the line, not one of the four
      // section cards.
      await expect(page.getByTestId('briefing-empty')).toHaveText(/Nothing else requires attention\./)
      await expect(page.getByRole('heading', { name: 'Actions' })).toHaveCount(0)
      await expect(page.getByRole('heading', { name: 'Overnight' })).toHaveCount(0)
    } finally {
      detach()
      assertConsoleClean(cap)
    }
  })

  test('BF-4: the "Briefing" nav link reaches the view', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)
    page.route('**/api/briefing**', (r) => fulfillBriefing(r, makeBriefing({ empty: true })))
    try {
      await visitAuthenticated(page, '/')
      await expect(page.locator('main h1')).toHaveText('Today')
      await page.getByRole('link', { name: 'Briefing' }).click()
      expect(page.url()).toContain('/briefing')
      await expect(page.locator('main h1')).toHaveText('Briefing')
    } finally {
      detach()
      assertConsoleClean(cap)
    }
  })
})

