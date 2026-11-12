import { expect, test, type Page, type Request, type Route } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * mail-safety-card.spec.ts — NerveShield Task 7.1 (R-020).
 *
 * The safety card on email rows of the Communications view (/search).
 * Deterministic via `page.route` mocks for the search and mail-security
 * endpoints.
 *
 * SC-1  Accessibility: the per-row toggle is a keyboard-operable button with
 *       aria-expanded/aria-controls; the card is a role=status region whose
 *       accessible name carries the disposition; details use a native
 *       <details> disclosure; non-email rows get no toggle; nothing is
 *       fetched until the user opens the card.
 * SC-2  Content: disposition, SHADOW, evidence-fidelity badge, reason codes,
 *       NOT_EVALUATED badges for checks not run, authentication results with
 *       provenance labels, axes semantics, findings.
 * SC-3  Security rendering: markup/injection strings from the assessment are
 *       rendered literally; no <img>/<script>/<iframe> is created, no script
 *       runs, and no request leaves the app origin.
 * SC-4  Fail closed: a safe 404 renders "Not assessed", no error banner.
 */

const COMM_ID = 'comm-mail-0123456789abcdef01234567'
const IMSG_ID = 'comm-imsg-1'
const IMG_INJECTION = '<img src="https://evil.example/pixel.png" onerror="window.__pwned=1">'
const SCRIPT_INJECTION = '<script>window.__pwned=2</script>'

const REMOTE: string[] = []

function rows() {
  const base = {
    person_id: null,
    direction: 'incoming',
    recipients: ['me@example.com'],
    timestamp: '2026-11-01T10:00:00Z',
    thread_id: null,
    attachments: [],
    sensitivity: 'normal',
  }
  return [
    {
      ...base,
      communication_id: COMM_ID,
      channel: 'email',
      sender: 'billing@unknown-vendor.example',
      content_ref: 'content://mail/x/y',
      classification: { subject: 'Invoice overdue' },
    },
    {
      ...base,
      communication_id: IMSG_ID,
      channel: 'imessage',
      sender: '+15550000000',
      content_ref: 'enc://imsg/1',
      classification: { subject: 'Invoice question' },
    },
  ]
}

const DETAIL = {
  assessment_id: 'as-7',
  account_scope: 'conn:x',
  disposition: 'REVIEW',
  mode: 'SHADOW',
  processing_state: 'ASSESSED',
  evidence_fidelity: 'BODY_ONLY',
  policy_version: 'p1',
  analysis_profile_version: 'a1',
  created_at: '2026-11-01T10:00:00Z',
  reason_codes: ['SENDER_UNVERIFIED', IMG_INJECTION],
  human_approval_required: true,
  action_capabilities: [],
  authentication: {
    spf: { result: 'PASS', provenance: 'PROVIDER_REPORTED' },
    dkim: { result: 'UNKNOWN', provenance: 'UNKNOWN' },
    dmarc: { result: null, provenance: null },
    arc: { result: 'FAIL', provenance: 'UNTRUSTED_HEADER' },
  },
  axes: {
    threat_likelihood: { semantics: 'RAW_SCORE', calibrated: false, value: null },
    urgency: { semantics: 'NOT_EVALUATED', calibrated: false, value: null },
  },
  completeness: {
    available: ['PARSE'],
    missing: ['DKIM_VERIFY'],
    checks_not_run: ['ATTACHMENT_SCAN'],
  },
  findings: [
    {
      finding_key: 'f1',
      rule_id: SCRIPT_INJECTION,
      category: 'SENDER_IMPERSONATION',
      severity: 'MEDIUM',
      status: 'OBSERVED',
      confidence_semantics: null,
      confidence_type: 'ABSENT',
      explanation_code: 'DISPLAY_NAME_MISMATCH',
      evidence_refs: ['ref:ev:1'],
    },
  ],
}

async function routeSearch(page: Page) {
  await page.route('**/api/communications/search?**', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        data: rows(),
        meta: {
          version: '1',
          request_id: 'r',
          timestamp: '2026-11-01T10:00:00Z',
          pagination: { total: 2 },
          search: {
            query: 'invoice',
            filters: {
              within_week: false,
              window_start: null,
              window_end: null,
              channel: null,
              request_intent: false,
              keywords: ['invoice'],
            },
            matched_keywords: ['invoice'],
            request_groups: [],
          },
        },
      }),
    }),
  )
}

function watch(page: Page) {
  const errors: string[] = []
  const securityCalls: string[] = []
  page.on('pageerror', (err) => errors.push(err.message))
  page.on('request', (req: Request) => {
    const url = new URL(req.url())
    if (url.origin !== 'http://localhost:3000' && url.origin !== 'http://127.0.0.1:3000') {
      REMOTE.push(req.url())
    }
    if (url.pathname.startsWith('/api/mail-security/')) securityCalls.push(url.pathname)
  })
  return { errors, securityCalls }
}

async function search(page: Page) {
  await seedBreakGlassSession(page)
  await page.goto('/search', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Search')
  await page.getByTestId('comm-within-week').uncheck()
  await page.getByTestId('comm-search-input').fill('invoice')
  await page.getByTestId('comm-search-submit').click()
  await expect(page.getByTestId('comm-results')).toBeVisible()
}

test.beforeEach(() => {
  REMOTE.length = 0
})

test.describe('NerveShield safety card (Task 7.1)', () => {
  test('SC-1..3 accessible, complete and inert', async ({ page }) => {
    const w = watch(page)
    await routeSearch(page)
    await page.route(
      `**/api/mail-security/assessments/for-communication/${COMM_ID}`,
      (route: Route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(DETAIL),
        }),
    )
    await search(page)

    // Non-email rows have no toggle; nothing fetched before the user asks.
    await expect(page.getByTestId(`comm-security-toggle-${IMSG_ID}`)).toHaveCount(0)
    const toggle = page.getByTestId(`comm-security-toggle-${COMM_ID}`)
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(toggle).toHaveAttribute('aria-controls', /.+/)
    expect(w.securityCalls).toEqual([])

    // Keyboard: focus + Enter opens it.
    await toggle.focus()
    await page.keyboard.press('Enter')
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const panelId = await toggle.getAttribute('aria-controls')
    const card = page.locator(`[id="${panelId}"]`).getByRole('status')
    await expect(card).toHaveAttribute('aria-label', /Security assessment: Review \(observation only/)
    await expect(card).toHaveAttribute('data-disposition', 'REVIEW')
    // Only the for-communication route is called (dev StrictMode may mount
    // the effect twice; the first run is cancelled).
    expect([...new Set(w.securityCalls)]).toEqual([
      `/api/mail-security/assessments/for-communication/${COMM_ID}`,
    ])

    // SC-2 content.
    await expect(card.getByTestId('mail-security-disposition')).toHaveText('Review')
    await expect(card).toContainText('SHADOW')
    await expect(card.getByTestId('mail-security-fidelity')).toHaveText('Body text only')
    await expect(card).toContainText('SENDER_UNVERIFIED')
    const notRun = card.getByTestId('mail-security-not-run')
    await expect(notRun).toContainText('ATTACHMENT_SCAN')
    await expect(notRun).toContainText('DKIM_VERIFY')
    await expect(notRun.getByTestId('mail-security-not-evaluated')).toHaveCount(2)

    // Details disclosure is a native <details>, opened from the keyboard.
    const summary = card.locator('summary')
    await summary.focus()
    await page.keyboard.press('Enter')
    await expect(card.getByTestId('mail-security-details')).toHaveAttribute('open', '')
    const auth = card.getByTestId('mail-security-auth')
    await expect(auth).toContainText('PASS')
    await expect(auth).toContainText('reported by mail provider')
    await expect(auth).toContainText('from an untrusted header')
    // dkim UNKNOWN + dmarc null → NOT_EVALUATED, never a fabricated result.
    await expect(auth.getByTestId('mail-security-not-evaluated')).toHaveCount(2)
    const axes = card.getByTestId('mail-security-axes')
    await expect(axes).toContainText('uncalibrated estimate (not a probability)')
    await expect(axes.getByTestId('mail-security-not-evaluated')).toHaveCount(1)
    await expect(card.getByTestId('mail-security-findings')).toContainText(
      'DISPLAY_NAME_MISMATCH',
    )
    await expect(card).toContainText('Nothing here approves anything')
    // Display only: no actionable control inside the card.
    await expect(card.getByRole('button')).toHaveCount(0)
    await expect(card.getByRole('link')).toHaveCount(0)

    // SC-3: injection strings shown literally, never as markup.
    await expect(card).toContainText(IMG_INJECTION)
    await expect(card).toContainText(SCRIPT_INJECTION)
    await expect(card.locator('img, script, iframe, object, embed')).toHaveCount(0)
    await page.waitForTimeout(500)
    expect(await page.evaluate(() => (window as { __pwned?: number }).__pwned)).toBeUndefined()
    expect(REMOTE, 'remote/third-party requests').toEqual([])
    expect(w.errors, 'uncaught exceptions').toEqual([])

    // Collapse again.
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(page.locator(`[id="${panelId}"]`)).toBeHidden()
  })

  test('SC-4 fail closed: safe 404 → Not assessed, no banner', async ({ page }) => {
    const w = watch(page)
    await routeSearch(page)
    await page.route(
      `**/api/mail-security/assessments/for-communication/${COMM_ID}`,
      (route: Route) =>
        route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: { error_code: 'not_found' } }),
        }),
    )
    await search(page)
    await page.getByTestId(`comm-security-toggle-${COMM_ID}`).click()
    const card = page.getByTestId('mail-security-indicator')
    await expect(card).toHaveAttribute('data-disposition', 'UNKNOWN')
    await expect(card.getByTestId('mail-security-disposition')).toHaveText('Not assessed')
    await expect(card.getByTestId('mail-security-details')).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
    expect(REMOTE).toEqual([])
    expect(w.errors).toEqual([])
  })
})
