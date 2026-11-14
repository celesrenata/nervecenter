import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * Calendar — connected accounts (prompt2 Phase P2, area 2c).
 *
 * Runs against the REAL dependency-free backend on :8000 (booted by
 * global-setup.mjs without a durable store) via the Vite proxy on :3000, with
 * `authToken` seeded in localStorage (mirrors settings.spec.ts).
 *
 * With no durable registry configured the backend is HONEST:
 *   - GET  /api/calendar/accounts        → 200 `{ data: [] }`  (empty, not an error)
 *   - POST /api/calendar/accounts        → 202 `{ executed: false, detail }` (not persisted)
 * so these assertions are deterministic and do NOT depend on a real DB:
 *   - the "Connected accounts" section renders and reports the honest empty state;
 *   - the connect form opens, and submitting yields an honest degraded message —
 *     never a fake success and never a fabricated account row.
 */

/** Seed a real break-glass session before any page script runs, then load the path. */
async function visitCalendar(page: import('@playwright/test').Page, path = '/calendar') {
  await seedBreakGlassSession(page)
  await page.goto(path, { waitUntil: 'domcontentloaded' })
}

test('calendar renders the connected-accounts section with an honest empty state', async ({
  page,
}) => {
  const errors: string[] = []
  const pageErrors: string[] = []
  const failedRequests: string[] = []
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
  page.on('pageerror', (e) => pageErrors.push(e.message))
  page.on('requestfailed', (r) => failedRequests.push(`${r.method()} ${r.url()}`))

  await visitCalendar(page)

  const section = page.getByTestId('calendar-accounts')
  await expect(section).toBeVisible()
  await expect(page.getByTestId('calendar-connect-toggle')).toBeVisible()

  // No durable store → the account list is empty; the UI says so honestly.
  await expect(
    section.getByText('No calendar accounts connected yet.', { exact: false }),
  ).toBeVisible()

  // The page itself did not throw and no required asset failed to load.
  expect(pageErrors, `page errors: ${pageErrors.join(' | ')}`).toEqual([])
  expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([])
  expect(
    failedRequests,
    `failed requests: ${failedRequests.join(' | ')}`,
  ).toEqual([])
})

test('connect form submits honestly (degraded write, no fake success or row)', async ({
  page,
}) => {
  await visitCalendar(page)

  await page.getByTestId('calendar-connect-toggle').click()
  const form = page.getByTestId('calendar-connect-form')
  await expect(form).toBeVisible()

  // The provider defaults to google (account picker); switch to CalDAV for the
  // manual form. Leave the credential blank so no secret ever transits.
  const select = form.locator('select').first()
  expect(await select.inputValue()).toBe('google')
  await select.selectOption('caldav')
  await form.getByPlaceholder('Work calendar').fill('Work calendar')

  await page.getByTestId('calendar-connect-submit').click()

  // Without durable storage the backend returns executed:false → the UI shows
  // an honest degraded message and does NOT pretend the account was saved.
  await expect(page.getByTestId('calendar-connect-error')).toBeVisible()
  await expect(page.getByText('No calendar accounts connected yet.', { exact: false })).toBeVisible()

  // No fabricated account row appears.
  await expect(page.locator('[data-testid^="calendar-account-"]')).toHaveCount(0)
})

test('google lists connected Gmail accounts and starts calendar consent on that connection', async ({
  page,
}) => {
  const created: unknown[] = []
  const authBodies: unknown[] = []
  await page.route('**/api/connections', (route) =>
    route.fulfill({
      json: {
        data: [
          { id: 'conn-gmail-1', kind: 'gmail', label: 'Gmail', config: { account: 'owner@example.com' } },
          { id: 'conn-relay-1', kind: 'relay_device', label: 'Mac', config: {} },
        ],
      },
    }),
  )
  await page.route('**/api/calendar/accounts', async (route) => {
    if (route.request().method() === 'POST') {
      created.push(route.request().postDataJSON())
      return route.fulfill({ status: 201, json: { id: 'cal-1', provider: 'google', config: {} } })
    }
    return route.fulfill({ json: { data: [], meta: {} } })
  })
  await page.route('**/api/connections/conn-gmail-1/auth', (route) => {
    authBodies.push(route.request().postDataJSON())
    return route.fulfill({
      json: { authorization_url: 'https://accounts.example.test/consent', state: 's', provider: 'google' },
    })
  })
  await page.route('https://accounts.example.test/**', (route) =>
    route.fulfill({ contentType: 'text/html', body: '<p>consent</p>' }),
  )

  await visitCalendar(page)
  await page.getByTestId('calendar-connect-toggle').click()
  const picker = page.getByTestId('calendar-google-accounts')
  await expect(picker.getByText('owner@example.com')).toBeVisible()
  // Only Gmail connections are offered; the token field is gone.
  await expect(page.locator('[data-testid^="calendar-use-connection-"]')).toHaveCount(1)
  await expect(page.getByTestId('calendar-connect-form').locator('input[type="password"]')).toHaveCount(0)

  await page.getByTestId('calendar-use-connection-conn-gmail-1').click()
  await page.waitForURL('https://accounts.example.test/consent')

  expect(created).toHaveLength(1)
  expect(created[0]).toMatchObject({ provider: 'google', connection_id: 'conn-gmail-1', label: 'owner@example.com' })
  expect(created[0]).not.toHaveProperty('credential')
  expect(authBodies).toEqual([{ extra: ['calendar'] }])
})
