import { expect, test } from '@playwright/test'

/**
 * waiting-on.spec.ts — the Waiting-On plane (prompt2 §19 — Waiting-On
 * Detection / §31 "what am I waiting for?").
 *
 * Deterministic via `page.route` API mocks (house style from views-a.spec.ts):
 * assertions do not depend on live backend seed state. Covers:
 *   WO-1  /waiting-on renders the seeded requests — person, description,
 *          the card-level "Waiting on" label, the elapsed-time badge, and the
 *          per-item "Follow up" / "Resolved" actions.
 *   WO-2  Clicking "Follow up" returns a rule-based *draft* that renders with
 *          the "never sent — review & send yourself" §13 framing.
 *   WO-3  Clicking "Resolved" closes the loop: the item leaves the OPEN list.
 *   WO-4  The "Waiting On" nav link reaches the view.
 *
 * Route-priority note: Playwright matches routes in reverse-registration
 * order (last registered wins). The specific action routes (follow-up,
 * resolve) are therefore registered AFTER the generic list route so they
 * take precedence for their own URLs; the list route is the catch-all.
 *
 * The dev server runs under React StrictMode, which double-invokes mount
 * effects — so the list mock must be idempotent (stateless), never a
 * one-shot counter.
 *
 * Console-error policy mirrors views-a.spec.ts: console errors, uncaught
 * page errors, and failed network requests all FAIL the test. Every request
 * these tests issue is under /api/waiting-on** and is mocked.
 */

// A request seeded by the backend's seed_demo() (NERVE_SEED_WAITING_ON=1):
// Sam, waiting 3 days for deployment credentials via messages.
const SAM_ITEM = {
  request_id: 'wo-seed-sam-creds',
  person_display_name: 'Sam',
  person_id: null,
  description: 'deployment credentials',
  requested_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
  source: 'messages',
  source_thread_id: 'thread-sam',
  status: 'open',
  attention_id: 'att-sam',
  follow_up_count: 0,
  last_follow_up_at: null,
  resolved_at: null,
  note: null,
  created_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
  updated_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
  // §19/§22 escalation policy (mirrors backend seed_demo): 3 days
  // unanswered → "follow up within 24h, or escalate to a VIP if unresolved".
  escalation_policy: { priority: 'urgent' },
}

function iso(daysAgo: number): string {
  return new Date(Date.now() - daysAgo * 24 * 60 * 60 * 1000).toISOString()
}

interface ConsoleCapture {
  errors: string[]
  pageErrors: string[]
  failedRequests: string[]
}

function attachConsoleCapture(page: import('@playwright/test').Page, cap: ConsoleCapture) {
  const onConsole = (msg: import('@playwright/test').ConsoleMessage) => {
    if (msg.type() !== 'error') return
    cap.errors.push(msg.text())
  }
  const onPageError = (err: Error) => {
    cap.pageErrors.push(String(err))
  }
  const onRequestFailed = (req: import('@playwright/test').Request) => {
    cap.failedRequests.push(`${req.method()} ${req.url()}`)
  }
  page.on('console', onConsole)
  page.on('pageerror', onPageError)
  page.on('requestfailed', onRequestFailed)
  return () => {
    page.off('console', onConsole)
    page.off('pageerror', onPageError)
    page.off('requestfailed', onRequestFailed)
  }
}

function assertConsoleClean(cap: ConsoleCapture) {
  expect(cap.pageErrors, 'uncaught exceptions').toEqual([])
  expect(cap.failedRequests, 'failed network requests').toEqual([])
  expect(cap.errors, 'console errors').toEqual([])
}

async function visitAuthenticated(page: import('@playwright/test').Page, path: string) {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'e2e-token')
  })
  await page.goto(path, { waitUntil: 'domcontentloaded' })
}

function fulfillJson(
  route: import('@playwright/test').Route,
  body: unknown,
  status = 200,
) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

test.describe('Waiting On (WO — prompt2 §19/§31)', () => {
  test('renders seeded requests with person, description, elapsed badge + actions', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Stateless (StrictMode idempotent) — always the one open request.
    await page.route('**/api/waiting-on**', (r) => fulfillJson(r, [SAM_ITEM]))

    try {
      await visitAuthenticated(page, '/waiting-on')

      await expect(page.getByRole('heading', { name: 'Waiting On' })).toBeVisible()
      // The §19 card renders the person, the thing requested, and the
      // card-level "Waiting on" label (exact — the nav link + h1 read
      // "Waiting On" with a capital O).
      await expect(page.getByText('Sam')).toBeVisible()
      await expect(page.getByText('deployment credentials')).toBeVisible()
      await expect(page.getByText('Waiting on', { exact: true })).toBeVisible()
      // The elapsed-time badge shows how long the request has been waiting.
      await expect(page.getByText('3 days')).toBeVisible()
      // The §22/§45 escalation indicator: this seeded request is urgent
      // ("follow up within 24h, or escalate to a VIP if unresolved").
      const urgentBadge = page.getByTestId('waiting-on-urgent-wo-seed-sam-creds')
      await expect(urgentBadge).toBeVisible()
      await expect(urgentBadge).toHaveText('Urgent')
      // The per-item actions are present for an OPEN request.
      await expect(page.getByRole('button', { name: 'Follow up' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Resolved' })).toBeVisible()

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('no urgent badge when the item has no urgent escalation policy', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Same request, but the relay posted no escalation policy → the card
    // still renders, just without the "Urgent" marker (§22/§45).
    await page.route('**/api/waiting-on**', (r) =>
      fulfillJson(r, [{ ...SAM_ITEM, escalation_policy: null }]),
    )

    try {
      await visitAuthenticated(page, '/waiting-on')
      await expect(page.getByText('Sam')).toBeVisible()
      const urgentBadge = page.getByTestId('waiting-on-urgent-wo-seed-sam-creds')
      await expect(urgentBadge).toHaveCount(0)
      // A policy that is *not* urgent (e.g. "standard") also does not badge.
      await page.route('**/api/waiting-on**', (r) =>
        fulfillJson(r, [{ ...SAM_ITEM, escalation_policy: { priority: 'standard' } }]),
      )
      await page.reload({ waitUntil: 'domcontentloaded' })
      await expect(urgentBadge).toHaveCount(0)
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('clicking "Follow up" returns a rule-based draft (never sent, §13)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // List first (catch-all), then the specific follow-up route so it wins.
    await page.route('**/api/waiting-on**', (r) =>
      fulfillJson(r, [{ ...SAM_ITEM, follow_up_count: 1 }]),
    )
    await page.route('**/api/waiting-on/wo-seed-sam-creds/follow-up**', (r) =>
      fulfillJson(r, {
        request_id: SAM_ITEM.request_id,
        draft: 'Hi Sam — following up on deployment credentials (3 days, no reply yet). Any update?',
        follow_up_count: 1,
        last_follow_up_at: iso(0),
        status: 'open',
        draft_only: true,
        basis: 'rule-based follow-up draft (never sent; §13)',
        new_attention: null,
      }),
    )

    const draftText =
      'Hi Sam — following up on deployment credentials (3 days, no reply yet). Any update?'

    try {
      await visitAuthenticated(page, '/waiting-on')
      await expect(page.getByText('Sam')).toBeVisible()
      await page.getByRole('button', { name: 'Follow up' }).click()

      // The draft renders, labelled as draft-only (never sent).
      await expect(page.getByText(draftText)).toBeVisible()
      await expect(page.getByText('never sent — review & send yourself')).toBeVisible()

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('clicking "Resolved" closes the loop — the item leaves the OPEN list', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Flip once the resolve request is intercepted; the re-list that follows
    // then returns the (now empty) OPEN list.
    let resolved = false
    // List first (catch-all), then the specific resolve route so it wins.
    await page.route('**/api/waiting-on**', (r) => fulfillJson(r, resolved ? [] : [SAM_ITEM]))
    await page.route('**/api/waiting-on/wo-seed-sam-creds/resolve**', (r) => {
      resolved = true
      return fulfillJson(r, {
        item: { ...SAM_ITEM, status: 'resolved', resolved_at: iso(0), note: 'got them' },
        attention_id: SAM_ITEM.attention_id,
        resolved: true,
      })
    })

    try {
      await visitAuthenticated(page, '/waiting-on')
      await expect(page.getByText('Sam')).toBeVisible()

      await page.getByRole('button', { name: 'Resolved' }).click()

      // The item is gone from the OPEN list; the view shows the empty state.
      await expect(page.getByText('Sam')).toHaveCount(0)
      await expect(
        page.getByText(/No waiting-on requests yet|Nothing waiting on you/),
      ).toBeVisible()

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('the "Waiting On" nav link reaches the view', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/waiting-on**', (r) => fulfillJson(r, []))

    try {
      await visitAuthenticated(page, '/now')
      // The sidebar exposes the plane.
      const link = page.getByRole('link', { name: 'Waiting On' })
      await expect(link).toBeVisible()
      await link.click()
      await expect(page.getByRole('heading', { name: 'Waiting On' })).toBeVisible()

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})
