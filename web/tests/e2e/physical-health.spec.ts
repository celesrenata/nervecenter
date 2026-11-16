import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Physical Health view with a mocked /api/health/summary (NS-ADR-100). */

const kind = (unit: string, values: number[]) => ({
  unit,
  count: values.length,
  latest: values.length ? { at: new Date().toISOString(), value: values[values.length - 1] } : null,
  mean: values.length ? values.reduce((a, b) => a + b, 0) / values.length : null,
  min: values.length ? Math.min(...values) : null,
  max: values.length ? Math.max(...values) : null,
  daily: values.map((v, i) => ({ date: `2026-01-${String(i + 1).padStart(2, '0')}`, value: v })),
})

const SUMMARY = {
  configured: true,
  days: 30,
  last_sync_at: new Date().toISOString(),
  total_samples: 9,
  kinds: {
    body_mass: kind('kg', [82.4, 82.1, 81.8]),
    sleep: kind('min', [410, 450]),
    step_count: kind('count', [8000, 9500]),
    heart_rate: kind('bpm', [70, 72]),
    resting_heart_rate: kind('bpm', []),
    hrv: kind('ms', []),
    active_energy: kind('kcal', []),
  },
}

test.describe('Physical Health', () => {
  test('renders weight, sleep, steps and heart rate from the summary', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(String(e)))
    await page.route('**/api/health/summary**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SUMMARY) }),
    )
    await seedBreakGlassSession(page)
    await page.goto('/health', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Physical Health' })).toBeVisible()
    const weight = page.getByTestId('health-card-Weight')
    await expect(weight).toContainText('81.8')
    await expect(weight).toContainText('kg')
    await expect(page.getByTestId('health-card-Steps')).toContainText('9,500')
    await expect(page.getByTestId('health-card-HRV')).toContainText('No samples')
    expect(errors).toEqual([])
  })

  test('shows an honest empty state before the phone has synced', async ({ page }) => {
    await page.route('**/api/health/summary**', (r) =>
      r.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ configured: true, days: 30, last_sync_at: null, total_samples: 0, kinds: {} }),
      }),
    )
    await seedBreakGlassSession(page)
    await page.goto('/health', { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('health-empty')).toBeVisible()
  })
})
