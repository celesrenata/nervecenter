import { expect, test, type Page, type Route } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * security-review.spec.ts — NerveShield Task 7.3 review queue view.
 *
 * Deterministic via `page.route` mocks of GET /api/mail-security/review-queue.
 *
 * SR-1  Server order is kept; important-human and commitment items show
 *       plain labels; commitments are shown as kept-for-review counts only;
 *       low-impact noise appears as a grouped digest; no action controls;
 *       no remote requests.
 * SR-2  Quiet hours: banner shown, interrupts held with a "held until" time,
 *       nothing dropped, no "new and high impact" badge.
 * SR-3  Fail closed: a safe 404 shows "Nothing needs review", no banner.
 */

const REMOTE: string[] = []

function item(over: Record<string, unknown>) {
  return {
    account_scope: 'conn:x',
    communication_id: null,
    created_at: '2026-11-11T13:00:00Z',
    score: 2,
    deliver_after: null,
    verification_advice: [],
    important_sender: false,
    commitments: [],
    ...over,
  }
}

const CRITICAL = item({
  assessment_id: 'as-crit',
  disposition: 'RESTRICTED',
  tier: 'CRITICAL',
  score: 6,
  delivery: 'INTERRUPT',
  reasons: [
    'BASE_RESTRICTED',
    'PAYMENT_OR_CREDENTIAL_ASK',
    'HIGH_SEVERITY_FINDING',
    'INTERRUPT_NOVEL_CRITICAL',
  ],
  verification_advice: ['VERIFY_PAYMENT_REQUEST'],
})

const VIP = item({
  assessment_id: 'as-vip',
  disposition: 'RESTRICTED',
  tier: 'HIGH',
  score: 5,
  delivery: 'QUEUE',
  reasons: ['BASE_RESTRICTED', 'IMPORTANT_HUMAN_SENDER', 'COMMITMENTS_PRESERVED'],
  important_sender: true,
  commitments: [
    { kind: 'commitment', ref_id: 'cm-1', status: 'REVIEW_CANDIDATE' },
    { kind: 'followup', ref_id: 'fu-1', status: 'REVIEW_CANDIDATE' },
  ],
})

async function routeQueue(page: Page, body: unknown, status = 200) {
  await page.route('**/api/mail-security/review-queue', (route: Route) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }),
  )
}

async function visit(page: Page) {
  page.on('request', (req) => {
    const url = new URL(req.url())
    if (url.origin !== 'http://localhost:3000' && url.origin !== 'http://127.0.0.1:3000') {
      REMOTE.push(req.url())
    }
  })
  await seedBreakGlassSession(page)
  await page.goto('/security-review', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Security review')
}

test.beforeEach(() => {
  REMOTE.length = 0
})

test.describe('NerveShield review queue (Task 7.3)', () => {
  test('SR-1 prioritized, labelled, digest, no actions', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))
    await routeQueue(page, {
      data: [CRITICAL, VIP],
      digest: { count: 7, by_reason: { INSPECTION_INCOMPLETE: 5, BULK_MAIL: 2 } },
      excluded_reviewed: 3,
      quiet_until: null,
      policy_version: 'mail-review-queue@1',
    })
    await visit(page)

    await expect(page.getByTestId('review-summary')).toContainText('2 to review')
    await expect(page.getByTestId('review-summary')).toContainText('nothing here acts')
    const rows = page.getByTestId('review-list').locator(':scope > li')
    await expect(rows).toHaveCount(2)
    await expect(rows.nth(0)).toHaveAttribute('data-testid', 'review-item-as-crit')
    await expect(rows.nth(0)).toContainText('Critical impact')
    await expect(rows.nth(0)).toContainText('New and high impact')
    await expect(rows.nth(0)).toContainText('Asks for money or credentials')

    const vip = page.getByTestId('review-item-as-vip')
    await expect(vip).toContainText('From someone you marked important')
    await expect(vip.getByTestId('review-item-commitments')).toHaveText(
      'Kept for review, not acted on: 1 commitment, 1 followup',
    )
    await expect(vip).not.toContainText('New and high impact')

    const digest = page.getByTestId('review-digest')
    await expect(digest).toContainText('7 lower-impact items, grouped')
    await expect(digest).toContainText('INSPECTION_INCOMPLETE × 5')

    // No control on the page acts on mail, trusts, approves or promotes.
    const names = await page.locator('main button').allInnerTexts()
    for (const n of names) {
      expect(n).toMatch(/^(Refresh|Show security card|Hide security card)$/)
    }
    // The card opens from the keyboard via the per-row disclosure.
    await page.route('**/api/mail-security/assessments/as-vip', (route: Route) =>
      route.fulfill({ status: 404, contentType: 'application/json', body: '{}' }),
    )
    const toggle = vip.getByRole('button', { name: /security card/ })
    await expect(toggle).toHaveText('Show security card')
    await toggle.focus()
    await page.keyboard.press('Enter')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await expect(vip.getByTestId('mail-security-indicator')).toBeVisible()

    expect(REMOTE).toEqual([])
    expect(errors).toEqual([])
  })

  test('SR-2 quiet hours hold interrupts and drop nothing', async ({ page }) => {
    const until = '2026-11-12T07:00:00Z'
    await routeQueue(page, {
      data: [
        {
          ...CRITICAL,
          delivery: 'QUEUE',
          reasons: [
            'BASE_RESTRICTED',
            'PAYMENT_OR_CREDENTIAL_ASK',
            'HIGH_SEVERITY_FINDING',
            'INTERRUPT_DEFERRED_QUIET_PERIOD',
          ],
          deliver_after: until,
        },
        VIP,
      ],
      digest: { count: 0, by_reason: {} },
      excluded_reviewed: 0,
      quiet_until: until,
      policy_version: 'mail-review-queue@1',
    })
    await visit(page)
    await expect(page.getByTestId('review-quiet')).toContainText('nothing is dropped')
    const crit = page.getByTestId('review-item-as-crit')
    await expect(crit).toHaveAttribute('data-delivery', 'QUEUE')
    await expect(crit).toContainText('Held for quiet hours')
    await expect(crit).toContainText('Held until')
    await expect(page.getByText('New and high impact')).toHaveCount(0)
    await expect(page.getByTestId('review-item-as-vip')).toBeVisible()
  })

  test('SR-3 fail closed on a safe 404', async ({ page }) => {
    await routeQueue(page, { detail: { error_code: 'not_found' } }, 404)
    await visit(page)
    await expect(page.getByTestId('review-empty')).toHaveText('Nothing needs review.')
    await expect(page.getByRole('alert')).toHaveCount(0)
    expect(REMOTE).toEqual([])
  })
})
