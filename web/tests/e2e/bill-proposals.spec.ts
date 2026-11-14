/**
 * Bills proposed from email (NS-ADR-095).
 *
 * Deterministic via `page.route` mocks (house style, waiting-on.spec.ts).
 * Covers:
 *   BP-1  Proposals render with payee, amount and due date; "Add bill" posts
 *         an accept and the proposal leaves the list.
 *   BP-2  A proposal with no due date keeps "Add bill" disabled until a date
 *         is entered, then sends that date.
 *   BP-3  "Dismiss" posts a reject.
 *   BP-4  No proposals: the section is not shown at all.
 *
 * Route priority: Playwright matches the LAST registered route first, so the
 * accept/reject routes are registered after the list route.
 */
import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

async function visitAuthenticated(page: import('@playwright/test').Page, path: string) {
  await seedBreakGlassSession(page)
  await page.goto(path, { waitUntil: 'domcontentloaded' })
}

function fulfillJson(route: import('@playwright/test').Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

const NOW = new Date().toISOString()

function candidate(id: string, payee: string, cents: number, due: string | null) {
  return {
    id,
    source_kind: 'email',
    source_id: `content://mail/c1/${id}`,
    status: 'proposed',
    extracted_fields: {
      payee: { value: payee, confidence: 0.85 },
      amount_cents: { value: cents, confidence: 0.97 },
      currency: { value: 'USD', confidence: 0.85 },
      ...(due ? { due_date: { value: due, confidence: 0.9 } } : {}),
    },
    evidence: [],
    needs_review: [],
    duplicate_of: null,
    bill_id: null,
    created_at: NOW,
    updated_at: NOW,
  }
}

const POWER = candidate('cand-power', 'City Power', 8420, '2026-12-20T12:00:00+00:00')
const CARD = candidate('cand-card', 'Example Card', 120410, null)

function billFrom(c: ReturnType<typeof candidate>, due: string) {
  return {
    id: `bill-${c.id}`,
    payee: c.extracted_fields.payee.value,
    amount_cents: String(c.extracted_fields.amount_cents.value),
    currency: 'USD',
    due_at: due,
    recurrence: 'one-time',
    interval: {},
    status: 'pending',
    confirmed_at: null,
    evidence: [],
    duplicate_of: null,
    created_at: NOW,
    updated_at: NOW,
  }
}

async function mockBills(page: import('@playwright/test').Page) {
  await page.route('**/api/bills/upcoming**', (r) => fulfillJson(r, { bills: [], days: 14, durable: true }))
  await page.route('**/api/bills/calendar**', (r) =>
    fulfillJson(r, { month: '2026-11', days: [], durable: true }),
  )
}

test.describe('Bills proposed from email (BP)', () => {
  test('BP-1 a proposal can be added as a bill', async ({ page }) => {
    let open = [POWER]
    const accepts: string[] = []
    await page.route('**/api/bills/?**', (r) => fulfillJson(r, { bills: [], durable: true }))
    await page.route('**/api/bills/', (r) => fulfillJson(r, { bills: [], durable: true }))
    await mockBills(page)
    await page.route('**/api/bills/candidates', (r) =>
      fulfillJson(r, { candidates: open, count: open.length, executed: true }),
    )
    await page.route('**/api/bills/candidates/*/accept', async (r) => {
      accepts.push(r.request().url())
      open = []
      return fulfillJson(r, {
        candidate: { ...POWER, status: 'accepted' },
        bill: billFrom(POWER, POWER.extracted_fields.due_date!.value),
        executed: true,
      })
    })

    await visitAuthenticated(page, '/bills')
    const section = page.getByRole('region', { name: 'Proposed from email' })
    await expect(section).toBeVisible()
    const item = section.getByTestId('bill-proposal')
    await expect(item).toContainText('City Power')
    await expect(item).toContainText('$84.20')
    await item.getByRole('button', { name: 'Add bill' }).click()
    await expect(page.getByRole('status')).toContainText('Added City Power as a bill')
    expect(accepts).toHaveLength(1)
    expect(accepts[0]).toContain('/api/bills/candidates/cand-power/accept')
    await expect(page.getByRole('region', { name: 'Proposed from email' })).toHaveCount(0)
  })

  test('BP-2 a missing due date must be filled in first', async ({ page }) => {
    const bodies: unknown[] = []
    await page.route('**/api/bills/?**', (r) => fulfillJson(r, { bills: [], durable: true }))
    await page.route('**/api/bills/', (r) => fulfillJson(r, { bills: [], durable: true }))
    await mockBills(page)
    await page.route('**/api/bills/candidates', (r) =>
      fulfillJson(r, { candidates: [CARD], count: 1, executed: true }),
    )
    await page.route('**/api/bills/candidates/*/accept', async (r) => {
      bodies.push(r.request().postDataJSON())
      return fulfillJson(r, {
        candidate: { ...CARD, status: 'accepted' },
        bill: billFrom(CARD, '2026-12-01T12:00:00Z'),
        executed: true,
      })
    })

    await visitAuthenticated(page, '/bills')
    const item = page.getByTestId('bill-proposal')
    await expect(item).toContainText('no due date found')
    const add = item.getByRole('button', { name: 'Add bill' })
    await expect(add).toBeDisabled()
    await item.getByLabel('Due date for Example Card').fill('2026-12-01')
    await expect(add).toBeEnabled()
    await add.click()
    await expect.poll(() => bodies.length).toBe(1)
    expect(bodies[0]).toEqual({ due_at: '2026-12-01T12:00:00Z' })
  })

  test('BP-3 dismiss sends a reject', async ({ page }) => {
    let open = [POWER]
    const rejects: string[] = []
    await page.route('**/api/bills/?**', (r) => fulfillJson(r, { bills: [], durable: true }))
    await page.route('**/api/bills/', (r) => fulfillJson(r, { bills: [], durable: true }))
    await mockBills(page)
    await page.route('**/api/bills/candidates', (r) =>
      fulfillJson(r, { candidates: open, count: open.length, executed: true }),
    )
    await page.route('**/api/bills/candidates/*/reject', async (r) => {
      rejects.push(r.request().url())
      open = []
      return fulfillJson(r, { candidate: { ...POWER, status: 'rejected' }, executed: true })
    })

    await visitAuthenticated(page, '/bills')
    await page.getByTestId('bill-proposal').getByRole('button', { name: 'Dismiss' }).click()
    await expect.poll(() => rejects.length).toBe(1)
    expect(rejects[0]).toContain('/api/bills/candidates/cand-power/reject')
    await expect(page.getByTestId('bill-proposal')).toHaveCount(0)
  })

  test('BP-4 no proposals, no section', async ({ page }) => {
    await page.route('**/api/bills/?**', (r) => fulfillJson(r, { bills: [], durable: true }))
    await page.route('**/api/bills/', (r) => fulfillJson(r, { bills: [], durable: true }))
    await mockBills(page)
    await page.route('**/api/bills/candidates', (r) =>
      fulfillJson(r, { candidates: [], count: 0, executed: true }),
    )
    await visitAuthenticated(page, '/bills')
    await expect(page.getByRole('heading', { name: 'Bills', exact: true })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Proposed from email' })).toHaveCount(0)
  })
})
