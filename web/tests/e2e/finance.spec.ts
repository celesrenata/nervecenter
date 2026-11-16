import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Finance view with a mocked /api/finance/* (NS-ADR-102). */
const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })
const result = (months: number, interest: number, saved: number | null, schedule = true) => ({
  never_reached: false, months, payoff_date: '2027-03-01', total_interest_cents: interest, remaining_cents: 0,
  interest_saved_cents: saved,
  ...(schedule ? { schedule: Array.from({ length: months }, (_, i) => ({
    month: i + 1, date: '2026-12-01', interest_cents: 100, balance_cents: 15000 - (i + 1) * 3750, payments: {},
  })) } : {}),
})
const PLAN = {
  extra_cents: 1000, total_balance_cents: 15000,
  minimums: { ...result(6, 802, null, false) },
  avalanche: result(4, 603, 199), snowball: result(4, 655, 147),
}
const ACCOUNT = {
  id: 'a1', institution: 'Chase', kind: 'credit_card', name: 'Sapphire', balance_cents: 15000,
  apr_pct: '24', min_payment_cents: 3000, due_day: 15, as_of: '2026-11-16',
}

test.describe('Finance', () => {
  test('shows debts, plan comparison, reminders and imports a file', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(String(e)))
    let imported = false
    let created: Record<string, unknown> | null = null
    await page.route('**/api/finance/accounts', async (r) => {
      if (r.request().method() === 'POST') {
        created = r.request().postDataJSON()
        return r.fulfill(json(ACCOUNT, 201))
      }
      return r.fulfill(json({ accounts: [ACCOUNT] }))
    })
    await page.route('**/api/finance/plan**', (r) => r.fulfill(json(PLAN)))
    await page.route('**/api/finance/reminders', (r) => r.fulfill(json({ reminders: [
      { kind: 'card_due', id: 'a1', title: 'Sapphire (Chase) payment due', due_date: '2026-11-18', days_until: 2, level: 2, amount_cents: 3000, amount_label: 'minimum payment' },
      { kind: 'bill', id: 'b1', title: 'Water bill', due_date: '2026-11-10', days_until: -6, level: 3, overdue: true, amount_cents: 4200, amount_label: 'amount due' },
    ] })))
    await page.route('**/api/finance/imports**', (r) => {
      imported = true
      return r.fulfill(json({ duplicate: false, added: 3, rows: 4, format: 'csv' }))
    })
    await seedBreakGlassSession(page)
    await page.goto('/finance', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Finance' })).toBeVisible()
    await expect(page.getByTestId('fin-accounts')).toContainText('Sapphire')
    await expect(page.getByTestId('fin-accounts')).toContainText('24% APR')
    await expect(page.getByTestId('fin-plan-avalanche')).toContainText('saves $1.99')
    await expect(page.getByTestId('fin-plan-snowball')).toContainText('saves $1.47')
    await expect(page.getByTestId('fin-plan-minimums')).toContainText('6 months')
    await expect(page.getByRole('img', { name: 'Remaining debt by month' })).toBeVisible()
    await expect(page.getByTestId('fin-reminder-card_due')).toContainText('due 2026-11-18')
    await expect(page.getByTestId('fin-reminder-bill')).toContainText('overdue since 2026-11-10')

    await page.getByLabel('Statement file').setInputFiles({
      name: 'statement.csv', mimeType: 'text/csv', buffer: Buffer.from('a,b\n1,2\n'),
    })
    await expect(page.getByTestId('fin-import-msg')).toContainText('Imported 3 of 4 rows.')
    expect(imported).toBe(true)

    const section = page.getByTestId('fin-accounts')
    await section.getByLabel('Name').fill('Freedom')
    await section.getByLabel('Balance ($)').fill('1200.50')
    await section.getByLabel('APR (%)').fill('19.99')
    await section.getByLabel('Minimum payment ($)').fill('35')
    await section.getByRole('button', { name: 'Add debt' }).click()
    await expect.poll(() => created).toMatchObject({ name: 'Freedom', balance_cents: 120050, min_payment_cents: 3500, apr_pct: '19.99' })
    expect(errors).toEqual([])
  })

  test('says so when a plan is never paid off', async ({ page }) => {
    const stuck = { never_reached: true, months: null, payoff_date: null, total_interest_cents: null, remaining_cents: 5, interest_saved_cents: null }
    await page.route('**/api/finance/accounts', (r) => r.fulfill(json({ accounts: [ACCOUNT] })))
    await page.route('**/api/finance/plan**', (r) => r.fulfill(json({ ...PLAN, minimums: stuck, avalanche: stuck, snowball: stuck })))
    await page.route('**/api/finance/reminders', (r) => r.fulfill(json({ reminders: [] })))
    await seedBreakGlassSession(page)
    await page.goto('/finance', { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('fin-plan-avalanche')).toContainText('Never paid off')
  })
})
