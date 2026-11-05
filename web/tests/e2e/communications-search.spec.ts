import { expect, test } from '@playwright/test'

/**
 * communications-search — cross-channel search UI e2e (R110 / §50, P1-107
 * gap B).
 *
 * Runs against the REAL dependency-free backend on :8000 (spawned by
 * `global-setup.mjs`, no DB, no `NERVE_SEED_*`) via the Vite dev-server
 * proxy (`VITE_API_BASE=/api`), the same transport the other specs in this
 * suite use. The in-memory communications registry is live (no durable plane
 * required for search), so the test SEEDS its own records through the real
 * ingest routes — never Playwright route-mocking — so the assertions are
 * deterministic against the real `/api/communications/search` envelope
 * (matches under top-level `data`, extras under `meta.search`).
 *
 * Asserted here:
 *   CS-1  Initial/unsearched state is distinct from "no matches"; a query
 *         that matches a freshly-ingested communication renders a result row
 *         with the sender/subject/channel from the normalized `data` shape.
 *   CS-2  A query that matches nothing renders the honest empty state.
 *
 * Console-error policy mirrors views.spec.ts (fail on console error /
 * pageerror / failed request, with the one documented /api/decisions filter).
 */

const KNOWN_MISSING_BACKEND_PATHS = ['/api/decisions']

/** Unique tokens so a re-run against a long-lived backend stays deterministic. */
const RUN = Date.now()
const SENDER = `search-e2e-${RUN}@example.com`
const SUBJECT = `E2E search subject prescription ${RUN}`
const QUERY = `prescription`

interface ConsoleCapture {
  errors: string[]
  pageErrors: string[]
  failedRequests: string[]
}

function attachConsoleCapture(
  page: import('@playwright/test').Page,
  cap: ConsoleCapture,
) {
  const onConsole = (msg: import('@playwright/test').ConsoleMessage) => {
    if (msg.type() !== 'error') return
    const text = msg.text()
    if (text.startsWith('Failed to load resource:')) {
      const url = msg.location()?.url ?? ''
      if (KNOWN_MISSING_BACKEND_PATHS.some((p) => url.endsWith(p))) return
    }
    if (text.includes('WebSocket') || (msg.location()?.url ?? '').includes('/ws/')) return
    cap.errors.push(text)
  }
  const onPageError = (err: Error) => cap.pageErrors.push(err.message)
  const onReqFailed = (req: import('@playwright/test').Request) =>
    cap.failedRequests.push(`${req.method()} ${req.url()}`)
  page.on('console', onConsole)
  page.on('pageerror', onPageError)
  page.on('requestfailed', onReqFailed)
  return () => {
    page.off('console', onConsole)
    page.off('pageerror', onPageError)
    page.off('requestfailed', onReqFailed)
  }
}

function assertClean(cap: ConsoleCapture) {
  expect(cap.pageErrors, 'uncaught exceptions').toEqual([])
  expect(cap.failedRequests, 'failed network requests').toEqual([])
  expect(cap.errors, 'console errors').toEqual([])
}

async function seedAuth(page: import('@playwright/test').Page) {
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

/** Seed one resolvable person + one matching communication via the real
 * ingest routes (the in-memory registry is live without a DB). */
async function seedCommunication(page: import('@playwright/test').Page) {
  // A person identity so the communication resolves to a person_id.
  const identity = await page.request.post('/api/people/identities/ingest', {
    data: { kind: 'email', value: SENDER, display_name: 'Search E2E Person' },
  })
  expect(identity.ok()).toBeTruthy()

  // One email communication whose subject carries the query keyword.
  const ingest = await page.request.post('/api/communications/ingest', {
    data: {
      channel: 'email',
      direction: 'incoming',
      sender: SENDER,
      content_ref: `enc://search-e2e/${RUN}`,
      classification: { subject: SUBJECT },
    },
  })
  expect(ingest.status()).toBe(201)
}

test('CS-1 search renders matched communications from the /search data shape', async ({
  page,
}) => {
  const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
  const detach = attachConsoleCapture(page, cap)
  try {
    await seedAuth(page)
    await seedCommunication(page)

    await page.goto('/search', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Search')

    // Initial/unsearched state is distinct from the "no matches" state.
    await expect(page.getByTestId('comm-initial-state')).toBeVisible()
    await expect(page.getByTestId('comm-empty-state')).toHaveCount(0)

    // Drive the search. within_week is off so the freshly-ingested record
    // (timestamped "now", but no horizon restriction needed) is matched
    // regardless of the default window.
    await page.getByTestId('comm-within-week').uncheck()
    await page.getByTestId('comm-search-input').fill(QUERY)

    const searchResponse = page.waitForResponse(
      (res) =>
        res.url().includes('/api/communications/search') &&
        res.request().method() === 'GET',
    )
    await page.getByTestId('comm-search-submit').click()
    const response = await searchResponse

    // Wire contract: normalized envelope — matches under `data`, extras under
    // `meta.search`, total == len(data).
    expect(response.status()).toBe(200)
    const body = await response.json()
    expect(Array.isArray(body.data)).toBeTruthy()
    expect(body.data.length).toBeGreaterThanOrEqual(1)
    expect(body.meta.search.query).toBe(QUERY)
    expect(body.meta.pagination.total).toBe(body.data.length)
    // §7: the matched rows carry no raw body.
    for (const row of body.data) {
      expect(row.body).toBeUndefined()
    }

    // The matched record renders in the results list with its sender.
    await expect(page.getByTestId('comm-results')).toBeVisible()
    await expect(page.getByText(SENDER, { exact: false })).toBeVisible()
    await expect(page.getByText(SUBJECT, { exact: false })).toBeVisible()
    // The initial-state placeholder is gone once a search has run.
    await expect(page.getByTestId('comm-initial-state')).toHaveCount(0)

    assertClean(cap)
  } finally {
    detach()
  }
})

test('CS-2 a non-matching query renders the honest empty state', async ({ page }) => {
  const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
  const detach = attachConsoleCapture(page, cap)
  try {
    await seedAuth(page)

    await page.goto('/search', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Search')
    await expect(page.getByTestId('comm-initial-state')).toBeVisible()

    await page.getByTestId('comm-within-week').uncheck()
    await page.getByTestId('comm-search-input').fill(`zzz-no-match-${RUN}`)

    const searchResponse = page.waitForResponse(
      (res) =>
        res.url().includes('/api/communications/search') &&
        res.request().method() === 'GET',
    )
    await page.getByTestId('comm-search-submit').click()
    const response = await searchResponse
    expect(response.status()).toBe(200)
    const body = await response.json()
    expect(body.data).toEqual([])
    expect(body.meta.pagination.total).toBe(0)

    // The empty state renders, distinct from the initial placeholder.
    await expect(page.getByTestId('comm-empty-state')).toBeVisible()
    await expect(page.getByTestId('comm-initial-state')).toHaveCount(0)

    assertClean(cap)
  } finally {
    detach()
  }
})
