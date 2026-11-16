import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Mental Health view with a mocked /api/mental-health/* (NS-ADR-101). */
const items = (n: number) => Array.from({ length: n }, (_, i) => `Item ${i + 1}`)
const choices = [0, 1, 2, 3].map((value) => ({ value, label: ['Not at all', 'Several days', 'More than half the days', 'Nearly every day'][value] }))
const INSTRUMENTS = [
  { key: 'phq9', title: 'PHQ-9', prompt: 'Over the last 2 weeks', items: items(9), choices, max_score: 27 },
  { key: 'gad7', title: 'GAD-7', prompt: 'Over the last 2 weeks', items: items(7), choices, max_score: 21 },
]
const daily = Array.from({ length: 10 }, (_, i) => ({ date: `2026-01-${String(i + 1).padStart(2, '0')}`, value: 4 + (i % 5) }))
const trend = (title: string, max: number) => ({
  title, max, count: 10,
  latest: { date: '2026-01-10', total: 8, band: 'mild' },
  daily, rolling_7: daily, rolling_30: daily,
  change_point: null, streaks: { threshold: 10, longest: 0, current: 0 },
})
const TRENDS = { days: 90, instruments: { phq9: trend('PHQ-9', 27), gad7: trend('GAD-7', 21) } }
const CORR = {
  days: 90, min_samples: 14, note: 'Associations between daily series. They do not show that one thing causes another.',
  rows: [
    { instrument: 'phq9', factor: 'sleep_minutes', label: 'Sleep duration', state: 'ok', n: 40, r: -0.52, lag: 1, min_samples: 14 },
    { instrument: 'phq9', factor: 'spend_dollars', label: 'Spending (paid bills)', state: 'too_few_samples', n: 5, r: null, lag: null, min_samples: 14 },
  ],
}
const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

test.describe('Mental Health', () => {
  test('submits a form, shows trends and factor table, and has no crisis wording', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(String(e)))
    let posted: { answers: number[]; comment?: string } | null = null
    await page.route('**/api/mental-health/instruments', (r) => r.fulfill(json({ instruments: INSTRUMENTS })))
    await page.route('**/api/mental-health/trends**', (r) => r.fulfill(json(TRENDS)))
    await page.route('**/api/mental-health/correlations**', (r) => r.fulfill(json(CORR)))
    await page.route('**/api/mental-health/forms', async (r) => {
      posted = r.request().postDataJSON()
      await r.fulfill(json({ total: 9, band: 'mild', max: 27, created: true }))
    })
    await seedBreakGlassSession(page)
    await page.goto('/mental-health', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Mental Health' })).toBeVisible()

    const form = page.getByTestId('mh-form-phq9')
    await expect(form.getByRole('button', { name: 'Save PHQ-9' })).toBeDisabled()
    for (let i = 0; i < 9; i++) await form.locator(`input[name="phq9-${i}"]`).nth(1).check()
    await form.getByLabel('Write-in (optional)').fill('slept badly')
    await form.getByRole('button', { name: 'Save PHQ-9' }).click()
    await expect(page.getByTestId('mh-saved-phq9')).toContainText('9 / 27')
    expect(posted).toEqual({ instrument: 'phq9', answers: Array(9).fill(1), comment: 'slept badly' })

    await expect(page.getByTestId('mh-trend-phq9')).toContainText('Latest 8 / 27')
    await expect(page.getByTestId('mh-row-phq9-sleep_minutes')).toContainText('-0.52')
    await expect(page.getByTestId('mh-row-phq9-spend_dollars')).toContainText('Too few samples')
    await expect(page.getByTestId('mh-patterns')).toContainText('1 day earlier')

    const text = (await page.locator('body').innerText()).toLowerCase()
    for (const phrase of ['crisis', 'hotline', 'lifeline', '988', 'seek help', 'call someone', 'get help']) {
      expect(text).not.toContain(phrase)
    }
    expect(errors).toEqual([])
  })
})
