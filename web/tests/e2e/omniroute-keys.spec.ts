import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * omniroute-keys.spec.ts — NS-ADR-118: Settings → OmniRoute. Keys are
 * write-only: the page shows set/not set and when it changed, never a value.
 * Driven by page.route mocks; the fake key values below exist only in this file.
 */
const json = (body: unknown, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

const REGULAR = 'fake-regular-key-123456'
const PRIVILEGED = 'fake-privileged-key-654321'

test('OmniRoute keys: set, test, clear; the value is never shown', async ({ page }) => {
  const state = {
    regular: { configured: false, source: 'none', vault_set: false, changed_at: null as string | null },
    privileged: { configured: true, source: 'env', vault_set: false, changed_at: null as string | null },
  }
  const audit: unknown[] = []
  const bodies: string[] = []
  const view = () => ({ keys: state, vault_writable: true, audit })

  await page.route('**/api/settings/omniroute', (r) => r.fulfill(json(view())))
  await page.route('**/api/settings/omniroute/keys/*/test', (r) => {
    const kind = r.request().url().split('/keys/')[1].split('/')[0]
    return r.fulfill(
      json(
        kind === 'regular'
          ? { key: 'regular', ok: true, inference: { ok: true, status: 'ok', routes: 7 } }
          : {
              key: 'privileged',
              ok: false,
              catalog: { ok: true, status: 'ok', skills: 950 },
              mcp: { ok: false, status: 'unauthorized' },
            },
      ),
    )
  })
  await page.route('**/api/settings/omniroute/keys/*', (r) => {
    const kind = r.request().url().split('/keys/')[1] as 'regular' | 'privileged'
    if (r.request().method() === 'PUT') {
      bodies.push(r.request().postData() ?? '')
      state[kind] = { configured: true, source: 'vault', vault_set: true, changed_at: '2026-01-01T12:00:00+00:00' }
      audit.unshift({ at: '2026-01-01T12:00:00+00:00', actor: 'owner', key: kind, action: 'set', outcome: 'ok' })
    } else {
      state[kind] = { configured: false, source: 'none', vault_set: false, changed_at: null }
      audit.unshift({ at: '2026-01-01T12:05:00+00:00', actor: 'owner', key: kind, action: 'clear', outcome: 'ok' })
    }
    return r.fulfill(json(view()))
  })

  await seedBreakGlassSession(page)
  await page.goto('/providers', { waitUntil: 'domcontentloaded' })

  await expect(page.getByTestId('omniroute-status-regular')).toContainText('Not set')
  await expect(page.getByTestId('omniroute-status-privileged')).toContainText('from the cluster Secret')
  await expect(page.getByTestId('omniroute-test-regular')).toBeDisabled()

  // Set the regular key.
  const input = page.getByTestId('omniroute-input-regular')
  await expect(input).toHaveAttribute('type', 'password')
  await input.fill(REGULAR)
  await page.getByTestId('omniroute-save-regular').click()
  await expect(page.getByTestId('omniroute-status-regular')).toContainText('stored in the encrypted vault')
  await expect(page.getByTestId('omniroute-status-regular')).toContainText('changed')
  await expect(input).toHaveValue('')
  expect(bodies).toEqual([JSON.stringify({ value: REGULAR })])
  expect(await page.content()).not.toContain(REGULAR)

  // Test buttons show counts and status only.
  await page.getByTestId('omniroute-test-regular').click()
  await expect(page.getByTestId('omniroute-result-regular')).toContainText('7 routes')
  await page.getByTestId('omniroute-test-privileged').click()
  await expect(page.getByTestId('omniroute-result-privileged')).toContainText('950 skills')
  await expect(page.getByTestId('omniroute-result-privileged')).toContainText('OmniRoute MCP: unauthorized')

  // Privileged key: set then clear falls back (mock clears to none).
  await page.getByTestId('omniroute-input-privileged').fill(PRIVILEGED)
  await page.getByTestId('omniroute-save-privileged').click()
  await page.getByTestId('omniroute-clear-privileged').click()
  await expect(page.getByTestId('omniroute-status-privileged')).toContainText('Not set')
  await expect(page.getByTestId('omniroute-audit')).toContainText('privileged key · clear')
  const html = await page.content()
  expect(html).not.toContain(REGULAR)
  expect(html).not.toContain(PRIVILEGED)
})

test('OmniRoute keys: a failed save shows the error without the value', async ({ page }) => {
  await page.route('**/api/settings/omniroute', (r) =>
    r.fulfill(
      json({
        keys: {
          regular: { configured: false, source: 'none', vault_set: false, changed_at: null },
          privileged: { configured: false, source: 'none', vault_set: false, changed_at: null },
        },
        vault_writable: true,
        audit: [],
      }),
    ),
  )
  await page.route('**/api/settings/omniroute/keys/regular', (r) =>
    r.fulfill(json({ detail: 'The key must be 8-512 printable characters.' }, 422)),
  )
  await seedBreakGlassSession(page)
  await page.goto('/providers', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('omniroute-input-regular').fill(REGULAR)
  await page.getByTestId('omniroute-save-regular').click()
  await expect(page.getByTestId('omniroute-error-regular')).toContainText('422')
  expect(await page.getByTestId('omniroute-error-regular').innerText()).not.toContain(REGULAR)
})

test('the OmniRoute keys live on the Providers page', async ({ page }) => {
  await page.route('**/api/settings/omniroute', (r) =>
    r.fulfill({ json: { keys: {
      regular: { configured: true, source: 'env', vault_set: false, changed_at: null },
      privileged: { configured: true, source: 'env', vault_set: false, changed_at: null },
      }, audit: [],
    } }),
  )
  await seedBreakGlassSession(page)
  await page.goto('/providers', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('providers-omniroute-keys')).toBeVisible()
  await expect(page.getByTestId('omniroute-input-privileged')).toBeVisible()
  await page.goto('/settings', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('tab', { name: 'OmniRoute' })).toHaveCount(0)
})
