import { expect, test, type Page, type Route, type Request } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * mail-security-indicator.spec.ts — NerveShield Phase 1, Task 1.11.
 *
 * Exercises the read-only SHADOW security indicator embedded in the Email
 * view. Deterministic via `page.route` API mocks (the views-a.spec.ts
 * convention), so the assertions do not depend on the Task 1.10 API serving
 * real assessment data (it is deny-by-default today).
 *
 * What it covers:
 *   - Task 7.1 regression: relay threads have no link to an assessment, so
 *     the card stays "Not assessed" and the view makes no mail-security
 *     request, even when an unrelated assessment exists. The populated card
 *     is covered by mail-safety-card.spec.ts (Communications view).
 *   - R-020.3 / T-05: rendering the view + indicator issues NO request to a
 *     remote / third-party origin — only same-origin `/api/...` XHRs (the one
 *     authorized Task 1.10 call plus the view's own email calls).
 *   - Fail-closed: when the Task 1.10 list is empty (the deny-by-default
 *     reality), the indicator shows a neutral "Not assessed" state and the
 *     thread view still renders — no error banner, no thrown exception.
 */

interface ConsoleCapture {
  errors: string[]
  pageErrors: string[]
  failedRequests: string[]
}

/** Any request whose origin is not the app's own (localhost:3000). */
const REMOTE_ORIGINS: string[] = []

function attachCaptures(page: Page, cap: ConsoleCapture) {
  const onConsole = (msg: import('@playwright/test').ConsoleMessage) => {
    if (msg.type() !== 'error') return
    cap.errors.push(msg.text())
  }
  const onPageError = (err: Error) => cap.pageErrors.push(err.message)
  const onReqFailed = (req: Request) =>
    cap.failedRequests.push(
      `${req.method()} ${req.url()} -> ${req.failure()?.errorText ?? 'unknown'}`,
    )
  // Record every request to an origin other than the app's own. The indicator
  // must cause zero third-party/remote fetches (R-020.3, T-05): no remote
  // image, font, iframe, script or beacon — only same-origin API XHRs.
  const onRequest = (req: Request) => {
    const url = new URL(req.url())
    const sameOrigin =
      url.origin === 'http://localhost:3000' ||
      url.origin === 'http://127.0.0.1:3000'
    if (!sameOrigin) REMOTE_ORIGINS.push(`${req.method()} ${req.url()}`)
  }
  page.on('console', onConsole)
  page.on('pageerror', onPageError)
  page.on('requestfailed', onReqFailed)
  page.on('request', onRequest)
  return () => {
    page.off('console', onConsole)
    page.off('pageerror', onPageError)
    page.off('requestfailed', onReqFailed)
    page.off('request', onRequest)
  }
}

/** A single seeded email thread + its one-message conversation. */
const THREAD = {
  thread_id: 'thr-sec-1',
  from_address: 'claims@unknown-insurer.example',
  to_address: 'me@example.com',
  sender_name: 'Claims Dept',
  subject: 'Action required on your policy',
  message_count: 1,
  unread: 1,
  last_body: 'Please confirm your details.',
  last_direction: 'RECEIVED',
  last_timestamp: '2026-10-08T00:00:00Z',
  attention_id: null,
}

const THREAD_DETAIL = {
  thread: THREAD,
  messages: [
    {
      message_id: 'm-1',
      thread_id: 'thr-sec-1',
      direction: 'RECEIVED',
      from_address: 'claims@unknown-insurer.example',
      to_address: 'me@example.com',
      subject: 'Action required on your policy',
      body: 'Please confirm your details.',
      timestamp: '2026-10-08T00:00:00Z',
    },
  ],
}

/** A redacted REVIEW assessment with reason codes + checks-not-run. */
const ASSESSMENT_DETAIL = {
  assessment_id: 'as-1',
  account_scope: 'acct-1',
  disposition: 'REVIEW',
  mode: 'SHADOW',
  processing_state: 'COMPLETE',
  evidence_fidelity: 'LOSSY',
  policy_version: 'p1',
  analysis_profile_version: 'a1',
  created_at: '2026-10-08T00:00:00Z',
  reason_codes: ['SENDER_UNVERIFIED', 'PROMPT_INJECTION_SUSPECTED'],
  human_approval_required: true,
  action_capabilities: [],
  authentication: {
    spf: { result: 'NONE', provenance: 'DERIVED' },
    dkim: { result: 'NONE', provenance: 'DERIVED' },
    dmarc: { result: 'NONE', provenance: 'DERIVED' },
    arc: { result: 'NONE', provenance: 'NONE' },
  },
  axes: {},
  completeness: {
    available: ['PARSE'],
    missing: ['DKIM_VERIFY'],
    checks_not_run: ['SEMANTIC_OBSERVATION'],
  },
}

async function routeEmail(page: Page) {
  await page.route('**/api/email/**', async (route: Route) => {
    const url = route.request().url()
    if (url.includes('/api/email/thr-sec-1')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(THREAD_DETAIL),
      })
    }
    // List endpoint.
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data: [THREAD], meta: {} }),
    })
  })
  await page.route('**/api/email', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data: [THREAD], meta: {} }),
    }),
  )
}

async function visitAuthenticated(page: Page, path: string) {
  await seedBreakGlassSession(page)
  await page.goto(path, { waitUntil: 'domcontentloaded' })
}

test.beforeEach(() => {
  REMOTE_ORIGINS.length = 0
})

test.describe('NerveShield security indicator (Task 1.11)', () => {
  test('Task 7.1 regression: a thread never shows an arbitrary assessment', async ({
    page,
  }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachCaptures(page, cap)
    const securityCalls: string[] = []
    page.on('request', (req) => {
      if (req.url().includes('/api/mail-security/')) securityCalls.push(req.url())
    })

    await routeEmail(page)
    // The old defect: the view took the first row of an unrelated list and
    // showed it on every thread. Serve such a row; it must NOT be used.
    await page.route('**/api/mail-security/assessments?**', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: [{ assessment_id: 'as-1', disposition: 'REVIEW' }],
          meta: {},
          next_cursor: null,
        }),
      }),
    )
    await page.route('**/api/mail-security/assessments/as-1', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(ASSESSMENT_DETAIL),
      }),
    )

    try {
      await visitAuthenticated(page, '/email')
      await expect(page.locator('main h1')).toHaveText('Email')
      await page.getByText('Claims Dept').click()

      const indicator = page.getByTestId('mail-security-indicator')
      await expect(indicator).toBeVisible()
      await expect(page.getByTestId('mail-security-disposition')).toHaveText(
        'Not assessed',
      )
      await expect(indicator).toHaveAttribute('data-disposition', 'UNKNOWN')
      await expect(indicator).toContainText('SHADOW')
      await expect(indicator).toHaveAttribute(
        'aria-label',
        /Security assessment: Not assessed/i,
      )
      await expect(indicator).not.toContainText('SENDER_UNVERIFIED')

      await page.waitForTimeout(500)
      expect(securityCalls, 'mail-security requests from Email view').toEqual([])
      expect(REMOTE_ORIGINS, 'remote/third-party requests').toEqual([])
      expect(cap.pageErrors, 'uncaught exceptions').toEqual([])
      expect(cap.failedRequests, 'failed network requests').toEqual([])
      expect(cap.errors, 'console errors').toEqual([])
    } finally {
      detach()
    }
  })

  test('fail-closed: empty Task 1.10 list → neutral "Not assessed", view intact', async ({
    page,
  }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachCaptures(page, cap)

    await routeEmail(page)
    // Deny-by-default reality: the list is empty (no account_scopes claim yet).
    await page.route('**/api/mail-security/assessments?**', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ data: [], meta: {}, next_cursor: null }),
      }),
    )

    try {
      await visitAuthenticated(page, '/email')
      await expect(page.locator('main h1')).toHaveText('Email')
      await page.getByText('Claims Dept').click()

      // Thread still renders.
      await expect(page.getByText('Please confirm your details.')).toBeVisible()

      // Indicator present but neutral — never a fabricated verdict.
      const indicator = page.getByTestId('mail-security-indicator')
      await expect(indicator).toBeVisible()
      await expect(
        page.getByTestId('mail-security-disposition'),
      ).toHaveText('Not assessed')
      await expect(indicator).toHaveAttribute('data-disposition', 'UNKNOWN')

      // No error banner was raised by the missing assessment.
      await expect(page.getByRole('alert')).toHaveCount(0)

      await page.waitForTimeout(500)
      expect(REMOTE_ORIGINS, 'remote/third-party requests').toEqual([])
      expect(cap.pageErrors, 'uncaught exceptions').toEqual([])
      expect(cap.failedRequests, 'failed network requests').toEqual([])
      expect(cap.errors, 'console errors').toEqual([])
    } finally {
      detach()
    }
  })
})
