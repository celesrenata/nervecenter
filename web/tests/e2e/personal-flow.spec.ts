import { expect, test } from '@playwright/test'

/**
 * personal-ops-recovery — degraded-backend e2e acceptance (workstream N).
 *
 * Runs against the REAL dependency-free backend on :8000 (spawned by
 * `global-setup.mjs` with `NERVE_MOCK_INFER=1`, no DB, no `NERVE_SEED_*`),
 * via the Vite dev-server proxy (`VITE_API_BASE=/api`, see
 * `playwright.config.ts`), so the browser client uses the real API and
 * **no demo records are seeded** (success criterion 14). The whole suite
 * asserts the *honest* degraded/in-memory surfaces, never fabricated
 * durable state — see `specs/personal-ops-recovery/acceptance.md` for the
 * row-by-row mapping and the durable-only divergences (cited unit tests).
 *
 * Asserted here:
 *   PF-1  Boot gate: Now + Today render their honest EMPTY states on a
 *         fresh, unseeded backend (criterion 14).
 *   PF-2  Connections: wizard pick→credentials → degraded write answers
 *         201 `executed:false` + the step error renders the backend
 *         detail; the list stays "No connections yet" with ZERO
 *         fabricated cards (criterion 15 / row 13, degraded surface).
 *   PF-3  Now feed: create → item lands in the NOW bucket (201,
 *         `status:"NOW"`, `due:null`); Snooze → item leaves the NOW
 *         feed and the wire response is `status:"TODAY"` with a `due`
 *         (criterion 11, in-memory surface).
 *   PF-4  Calendar: ingest → event renders in CalendarView "Upcoming"
 *         (criterion 7, live in-memory calendar surface).
 *   PF-5  Bills: degraded badge + empty states + `confirm-paid` wire
 *         contract 202 `executed:false, reason:"persistence_not_configured"`
 *         (criterion 8, degraded surface).
 *   PF-6  Durable-only Today divergence: `GET /api/today` is still
 *         all-empty even though the in-memory attention store now holds a
 *         TODAY item, and Low Energy Mode degrades to the honest
 *         "no durable storage configured" error (spec §2 criterion 7/11).
 *
 * Console-error policy: identical to `ask.spec.ts` — fail on any console
 * error, pageerror, or failed request; the single documented filter is a
 * `Failed to load resource` whose `msg.location().url` ends in
 * `/api/decisions` (a real 501 that DecisionsView degrades on without an
 * application error). Every route touched by this suite answers 2xx, so
 * the filter never fires on it.
 *
 * Test order within this file is meaningful (serial, single worker):
 * PF-6's divergence assertion depends on PF-3 having left an item in the
 * in-memory attention store.
 */

const KNOWN_MISSING_BACKEND_PATHS = ['/api/decisions']

/** Unique reason/title so a re-run against a long-lived backend (if the
 *  teardown is ever skipped) can still be located deterministically. */
const ATTENTION_REASON = `E2E accept: sign the NDA before the client call (run ${Date.now()})`
const EVENT_ID = 'e2e-accept-001'
const EVENT_TITLE = 'E2E accept: client call'

interface ConsolePolicy {
  consoleErrors: string[]
  pageErrors: string[]
  failedRequests: string[]
  detach: () => void
}

/** Attach the shared console/pageerror/requestfailed capture (ask.spec.ts). */
function captureConsoleErrors(
  page: import('@playwright/test').Page,
): ConsolePolicy {
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
  const onPageError = (err: Error) => pageErrors.push(err.message)
  const onRequestFailed = (req: import('@playwright/test').Request) =>
    failedRequests.push(`${req.method()} ${req.url()}`)
  page.on('console', onConsole)
  page.on('pageerror', onPageError)
  page.on('requestfailed', onRequestFailed)
  return {
    consoleErrors,
    pageErrors,
    failedRequests,
    detach: () => {
      page.off('console', onConsole)
      page.off('pageerror', onPageError)
      page.off('requestfailed', onRequestFailed)
    },
  }
}

/** Fail the test with the full captures if any channel recorded. */
function assertClean({
  consoleErrors,
  pageErrors,
  failedRequests,
}: ConsolePolicy) {
  expect(pageErrors, 'uncaught exceptions').toEqual([])
  expect(failedRequests, 'failed network requests').toEqual([])
  expect(consoleErrors, 'console errors').toEqual([])
}

/** Seed the same (non-enforced) token convention the AC005 suite uses. */
async function seedAuth(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'e2e-token')
  })
}

test('PF-1 boot gate: Now + Today render honest empty states (no demo records)', async ({
  page,
}) => {
  const policy = captureConsoleErrors(page)
  try {
    await seedAuth(page)
    // — Now: fresh backend, NOW bucket empty (no NERVE_SEED_ATTENTION).
    await page.goto('/now', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Now')
    await expect(page.getByRole('tab', { name: 'NOW', exact: true })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await expect(page.getByText('bucket right now')).toBeVisible()
    await expect(page.locator('ul[aria-live="polite"] > li')).toHaveCount(0)

    // — Today: all three sections honest-empty; connections empty too.
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Today')
    await expect(page.getByText('Nothing needs you right now.')).toBeVisible()
    await expect(page.getByText('Nothing scheduled in the next 48 hours.')).toBeVisible()
    await expect(page.getByText('Nothing handled in the last 24 hours.')).toBeVisible()
    await expect(page.getByText('No connections configured yet.')).toBeVisible()

    assertClean(policy)
  } finally {
    policy.detach()
  }
})

test('PF-2 connections: degraded write + zero fabricated cards', async ({ page }) => {
  const policy = captureConsoleErrors(page)
  try {
    await seedAuth(page)
    await page.goto('/connections', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Connection Center')
    // The real backend answers (not the in-browser mock), so the mock-mode
    // banner must be ABSENT, and the honest empty state present instead.
    await expect(page.getByText('mock mode')).toHaveCount(0)
    await expect(page.getByText('No connections yet')).toBeVisible()
    // Zero fabricated connection cards.
    await expect(page.locator('article, [class*="ConnectionCard"]')).toHaveCount(0)

    // Wizard: pick the relay preset (skips verify), fill the only field.
    await page.getByRole('button', { name: 'Add your first connection' }).click()
    await expect(
      page.getByRole('dialog', { name: 'Add connection wizard' }),
    ).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Add a connection' })).toBeVisible()
    // Selecting the preset advances straight to credentials — pickKind sets
    // the step to nextStep(...) (AddConnectionWizard.tsx:114-118), so there is
    // no intermediate "Continue" click for this flow.
    await page.getByRole('radio', { name: 'Apple Messages relay' }).click()
    // Credentials step for the relay preset (heading: "<label> — Credentials").
    await expect(
      page.getByRole('heading', { name: 'Apple Messages relay — Credentials' }),
    ).toBeVisible()
    await page.getByLabel('Device name').fill('Living-room Mac')

    // Submit → the only write path on the degraded backend.
    const createResponse = page.waitForResponse(
      (res) =>
        res.url().endsWith('/api/connections') && res.request().method() === 'POST',
    )
    await page.getByRole('button', { name: 'Save & start sign-in' }).click()
    const response = await createResponse

    // Wire contract: 201 but an honest degraded write (Area 4).
    expect(response.status()).toBe(201)
    const body = await response.json()
    expect(body.accepted).toBe(true)
    expect(body.executed).toBe(false)
    expect(typeof body.request_id).toBe('string')
    expect(typeof body.timestamp).toBe('string')
    expect(body.detail).toBe('durable storage is not configured')

    // The wizard stays on the credentials step and renders the backend
    // detail in its step error (role="alert") — no fabricated created row.
    await expect(
      page.getByRole('dialog', { name: 'Add connection wizard' }).getByRole('alert'),
    ).toHaveText('durable storage is not configured')
    await expect(
      page.getByRole('heading', { name: 'Apple Messages relay — Credentials' }),
    ).toBeVisible()

    // The list is still the honest empty list — zero fabricated cards.
    await expect(page.getByText('No connections yet')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add connection' })).toBeVisible()

    assertClean(policy)
  } finally {
    policy.detach()
  }
})

test('PF-3 now feed: create lands in NOW; snooze moves it to TODAY', async ({ page }) => {
  const policy = captureConsoleErrors(page)
  let attentionId = ''
  try {
    await seedAuth(page)
    await page.goto('/now', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Now')
    await expect(page.getByText('bucket right now')).toBeVisible()

    // — Create through the UI form (reason + default urgency/importance 0.5,
    //   no `due`, no `status` — the backend defaults new items to NOW).
    await page.getByTestId('now-reason-input').fill(ATTENTION_REASON)
    const createResponse = page.waitForResponse(
      (res) =>
        res.url().endsWith('/api/attention') &&
        res.request().method() === 'POST' &&
        !/\/(now|now\/)$/.test(res.url().split('/api/').pop() ?? ''),
    )
    await page.getByRole('button', { name: 'Add item' }).click()
    const create = await (await createResponse).json()

    expect(create.status).toBe('NOW')
    expect(create.due).toBeNull()
    expect(typeof create.attention_id).toBe('string')
    attentionId = create.attention_id
    expect(typeof create.priority).toBe('number')

    // The item renders in the NOW feed with its reason text.
    const card = page.locator(`[data-testid="attention-${attentionId}"]`)
    await expect(card).toBeVisible()
    await expect(card.getByText(ATTENTION_REASON)).toBeVisible()
    // NowView renders the singular count ("1 now item") for exactly one item.
    await expect(page.getByText('1 now item', { exact: true })).toBeVisible()

    // — Snooze: the wire response moves the item NOW → TODAY and sets a `due`;
    //   the NOW feed then reloads and the card has left it.
    const snoozeResponse = page.waitForResponse(
      (res) =>
        res.url().endsWith(`/api/attention/${attentionId}/snooze`) &&
        res.request().method() === 'POST',
    )
    await card.getByRole('button', { name: 'Snooze' }).click()
    const snoozed = await (await snoozeResponse).json()
    expect(snoozed.status).toBe('TODAY')
    expect(typeof snoozed.due).toBe('string')
    expect(snoozed.due.length).toBeGreaterThan(0)

    await expect(card).toHaveCount(0)
    await expect(page.getByText('bucket right now')).toBeVisible()
    await expect(page.getByText('0 now items')).toBeVisible()

    assertClean(policy)
  } finally {
    policy.detach()
  }
})

test('PF-4 calendar: ingest renders in Upcoming (in-memory surface)', async ({
  page,
}) => {
  const policy = captureConsoleErrors(page)
  try {
    await seedAuth(page)
    await page.goto('/calendar', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Calendar')
    // Fresh backend → the honest empty schedule.
    await expect(page.getByText('Nothing on the calendar in the next 7 days')).toBeVisible()

    // Ingest a live +2h..+3h event through the wire (CalendarView is
    // read-only — ingest is calendar-sync driven).
    const start = new Date(Date.now() + 2 * 60 * 60 * 1000)
    const end = new Date(start.getTime() + 60 * 60 * 1000)
    const iso = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, 'Z')
    const ingestRes = await page.request.post('/api/calendar/ingest', {
      data: {
        event_id: EVENT_ID,
        title: EVENT_TITLE,
        start: iso(start),
        end: iso(end),
        location: 'Zoom',
      },
    })
    expect(ingestRes.status()).toBe(201)
    const ingested = await ingestRes.json()
    expect(ingested.event_id).toBe(EVENT_ID)
    expect(ingested.title).toBe(EVENT_TITLE)

    // Reload → the event is in the 7-day Upcoming window.
    await page.reload({ waitUntil: 'domcontentloaded' })
    const event = page.getByText(EVENT_TITLE, { exact: true })
    await expect(event).toBeVisible()
    await expect(page.getByText('Nothing on the calendar in the next 7 days')).toHaveCount(0)
    await expect(page.getByText('1 upcoming event')).toBeVisible()

    assertClean(policy)
  } finally {
    policy.detach()
  }
})

test('PF-5 bills: degraded badge + empty states + honest confirm-paid wire', async ({
  page,
}) => {
  const policy = captureConsoleErrors(page)
  try {
    await seedAuth(page)
    await page.goto('/bills', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Bills')
    // Durable storage absent → the honest "demo mode — not persisted" badge
    // and empty states (no fabricated bills).
    await expect(page.getByText('demo mode — not persisted')).toBeVisible()
    await expect(page.getByText('No bills yet')).toBeVisible()
    await expect(page.getByText('Nothing due in the next two weeks.')).toBeVisible()

    // The ONLY paid-state writer is POST /api/bills/{id}/confirm-paid; on the
    // degraded backend it short-circuits to the honest 202 before any lookup
    // (BillsView would flash "Not recorded — bills storage is not
    // configured." for a real row — web/src/views/BillsView.tsx:401-411).
    const confirmRes = await page.request.post(
      '/api/bills/e2e-accept-degraded/confirm-paid',
    )
    expect(confirmRes.status()).toBe(202)
    const confirm = await confirmRes.json()
    expect(confirm.executed).toBe(false)
    expect(confirm.reason).toBe('persistence_not_configured')

    assertClean(policy)
  } finally {
    policy.detach()
  }
})

test('PF-6 durable-only Today divergence + Low Energy Mode degradation', async ({
  page,
}) => {
  const policy = captureConsoleErrors(page)
  try {
    await seedAuth(page)
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    await expect(page.locator('main h1')).toHaveText('Today')

    // The in-memory attention store now holds the PF-3 item (status TODAY,
    // due +1 day) — yet GET /api/today is still all-empty, because Today's
    // three providers read the durable repository_registry (None without a
    // DB). This is the sanctioned durable-only divergence: the unit tests
    // tests/backend/test_today.py::test_coming_up_calendar_window /
    // test_needs_you_ordering_importance_then_deadline pin the durable
    // behavior; the e2e asserts the honest degraded surface.
    const today = await page.request.get('/api/today')
    expect(today.status()).toBe(200)
    const body = await today.json()
    expect(body.needs_you).toEqual([])
    expect(body.coming_up).toEqual([])
    expect(body.handled).toEqual([])
    expect(body.low_energy).toBe(false)

    // The DOM agrees: every section renders its honest empty state.
    await expect(page.getByText('Nothing needs you right now.')).toBeVisible()
    await expect(page.getByText('Nothing scheduled in the next 48 hours.')).toBeVisible()
    await expect(page.getByText('Nothing handled in the last 24 hours.')).toBeVisible()

    // Low Energy Mode: PATCH /api/today/mode degrades to
    // {accepted:true, executed:false, low_energy:false, detail} and the view
    // surfaces the honest error instead of a silent toggle.
    const modeResponse = page.waitForResponse(
      (res) =>
        res.url().endsWith('/api/today/mode') && res.request().method() === 'PATCH',
    )
    await page.getByRole('switch', { name: 'Low Energy Mode' }).click()
    const mode = await (await modeResponse).json()
    expect(mode.accepted).toBe(true)
    expect(mode.executed).toBe(false)
    expect(mode.low_energy).toBe(false)
    expect(mode.detail).toBe('durable storage is not configured')
    await expect(
      page.getByText('Low Energy Mode is not available right now — no durable storage configured.'),
    ).toBeVisible()

    assertClean(policy)
  } finally {
    policy.detach()
  }
})
