import { expect, test } from '@playwright/test'

/**
 * Settings — browser-local preferences grouped by feature set.
 *
 * Runs against the REAL backend on :8000 via the Vite proxy, with `authToken`
 * seeded in localStorage (mirrors views.spec.ts). The settings panel makes no
 * live API calls — appearance and workflow values are local-only, and the
 * connections category is static instruction content — so the assertions are
 * deterministic and do not depend on the network.
 *
 * Coverage:
 *   - The route renders under the auth gate and the centered category tabs.
 *   - Workflow, data, and connections categories render their own panels.
 *   - Global attributes (accent, font scale, reduce-motion) apply live to
 *     `<html>` and survive a reload (persistence).
 *   - The home-view preference changes what `/` renders.
 *   - The connector + download/sideload instruction content renders.
 */

/** The dark emerald accent token, as defined in SettingsContext. */
const EMERALD_DARK_ACCENT = '#10b981'

/** Seed the auth token before any page script runs, then load the path. */
async function visitSettings(page: import('@playwright/test').Page, path = '/settings') {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'e2e-token')
  })
  await page.goto(path, { waitUntil: 'domcontentloaded' })
}

test('settings renders under the auth gate with categorized tabs', async ({ page }) => {
  const errors: string[] = []
  const pageErrors: string[] = []
  const failedRequests: string[] = []
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
  page.on('pageerror', (e) => pageErrors.push(e.message))
  page.on('requestfailed', (r) => failedRequests.push(`${r.url()}`))

  await visitSettings(page)

  // The auth gate passed (an unauthenticated user would land on /login).
  await expect(page.locator('main h1')).toHaveText('Settings')

  // The sidebar nav item and centered category tabs are wired.
  await expect(page.getByRole('link', { name: 'Settings' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Appearance' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Workflow' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Data & privacy' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Connections & apps' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Appearance' })).toBeVisible()

  // Feature defaults are separated into their own categories.
  await page.getByRole('tab', { name: 'Workflow' }).click()
  await expect(page.getByRole('heading', { name: 'Workflow defaults' })).toBeVisible()
  await expect(page.getByText('Default briefing')).toBeVisible()
  await expect(page.getByText('Home view')).toBeVisible()
  await page.getByRole('tab', { name: 'Data & privacy' }).click()
  await expect(page.getByRole('heading', { name: 'Data & privacy' })).toBeVisible()
  await expect(page.getByText('Wellness trend window')).toBeVisible()
  await page.getByRole('tab', { name: 'Connections & apps' }).click()

  // Connections & apps — connector usage instructions (static content). The register
  // endpoint is `exact` because the verify endpoint shares the prefix.
  await expect(page.getByRole('heading', { name: 'Connectors' })).toBeVisible()
  await expect(page.getByText('POST /api/mail-connectors', { exact: true })).toBeVisible()

  // Section 3 — download / sideload instructions (static content).
  await expect(page.getByRole('heading', { name: /download.*sideload/i })).toBeVisible()
  await expect(page.getByText('devicectl')).toBeVisible()
  // The panel links through to the live Downloads manifest. Scope to the
  // panel's own <a> (the sidebar nav has its own /downloads link, so the
  // bare selector resolves to two elements and violates strict mode).
  await expect(page.getByRole('main').getByRole('link', { name: 'Downloads' })).toBeVisible()

  // No application-level console/page errors or failed requests.
  expect(pageErrors, `page errors: ${pageErrors.join('; ')}`).toEqual([])
  expect(errors, `console errors: ${errors.join('; ')}`).toEqual([])
  expect(failedRequests, `failed requests: ${failedRequests.join('; ')}`).toEqual([])
})

test('global attributes apply live to <html> and persist across a reload', async ({ page }) => {
  await visitSettings(page)
  await expect(page.locator('main h1')).toHaveText('Settings')

  // Pick a deterministic theme so the accent palette resolves to the dark set.
  await page.getByRole('radio', { name: 'Dark', exact: true }).click()
  await expect(page).toHaveURL(/\/settings$/)
  await expect.poll(async () => page.evaluate(() => document.documentElement.getAttribute('data-theme')))
    .toBe('dark')

  // Accent — live override of the --accent token on the root element.
  await page.getByRole('radio', { name: 'Emerald', exact: true }).click()
  await expect
    .poll(async () => page.evaluate(() => document.documentElement.style.getPropertyValue('--accent')))
    .toBe(EMERALD_DARK_ACCENT)

  // Font scale — root font-size drives the whole UI.
  await page.getByRole('radio', { name: 'Large', exact: true }).click()
  await expect
    .poll(async () => page.evaluate(() => document.documentElement.style.fontSize))
    .toBe('17.6px')

  // Reduce motion — the data attribute the CSS rule keys off.
  await page.getByRole('radio', { name: 'Reduce', exact: true }).click()
  await expect
    .poll(async () => page.evaluate(() => document.documentElement.getAttribute('data-reduced-motion')))
    .toBe('reduce')

  // Reload: the stored settings (and theme) re-apply before React paints.
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Settings')

  await expect.poll(async () => page.evaluate(() => document.documentElement.getAttribute('data-theme')))
    .toBe('dark')
  await expect
    .poll(async () => page.evaluate(() => document.documentElement.style.getPropertyValue('--accent')))
    .toBe(EMERALD_DARK_ACCENT)
  await expect
    .poll(async () => page.evaluate(() => document.documentElement.style.fontSize))
    .toBe('17.6px')
  await expect
    .poll(async () => page.evaluate(() => document.documentElement.getAttribute('data-reduced-motion')))
    .toBe('reduce')
})

test('home-view preference changes what / renders', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'e2e-token')
    localStorage.setItem('nerve-settings', JSON.stringify({ landingPage: 'overview' }))
  })
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Overview')
})
