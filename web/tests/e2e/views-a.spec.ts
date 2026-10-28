import { expect, test } from '@playwright/test'

/**
 * views-a.spec.ts — gap-closure round for the NerveCenter web audit (OV/SY/TA/GL).
 *
 * Complements `views.spec.ts` (which asserts the ten views *render*). This
 * suite asserts the *new behavior* added in this round, deterministically via
 * `page.route` API mocks so the assertions do not depend on live backend
 * state (which is mid-wiring: /api/systems 501, /api/tasks 500, no WS route).
 *
 * What it covers:
 *   OV-2  Overview: no "Goal 1/2/3" hardcodes; real goal/agent panels.
 *   OV-1  Overview: `role=alert` banner shows `status — detail` on a 5xx.
 *   SY-2  Systems: honest connection badge (text + aria-label, never a bare dot).
 *   SY-3  Systems: `role=alert` banner surfaces the `501 — …` detail.
 *   TA-3  Task DAG: renders from real `id`/`title`/`status` (lowercase) fields.
 *   TA-2  Task DAG: dependency graph (SVG edges) + concurrency badges.
 *   GL-1  Goals: "+ New Goal" opens a form and POSTs, then refreshes the list.
 *   GL-2  Goals: real progress/status/due-date + empty-state guidance copy.
 *   GL-3  Goals: inline-confirm delete issues DELETE /api/goals/{id} (204)
 *           and removes the row; cancel dismisses without a request.
 *
 * Console-error policy mirrors `views.spec.ts`:
 *   - `page.on('console')` type 'error' FAILs the test, EXCEPT the two
 *     documented resource-load filters: `/api/systems`, `/api/decisions`
 *     (known-missing on the mock backend), and WebSocket resource failures
 *     (`/ws/*`) — this round opens a real socket for the honest connection
 *     badge, and the mock backend has no /ws route, so the socket failure is
 *     provably unrelated to view rendering and is handled in-state.
 *   - `page.on('pageerror')` and `page.on('requestfailed')` always FAIL.
 */

// /api/tasks is additionally tolerated: the OV-1 test below deliberately
// induces a 500 on /api/tasks (page.route → status 500) to verify the
// role=alert banner, so the browser's resource-load error for that induced
// response is expected and must not fail the console-capture.
// Canonical endpoints (client contract as of e59bc39): the list GETs for
// systems now issue `GET /api/systems/` — the slashless form answers with a
// 307 whose absolute Location drops the port, so the client always uses the
// trailing-slash form (see apiFetch callers in client.ts). Resource-load
// errors for those canonical URLs are filtered, as are the deliberately
// induced 5xx responses.
const KNOWN_MISSING_BACKEND_PATHS = ['/api/systems/', '/api/systems', '/api/decisions', '/api/tasks']

/** The view's live badge socket (SY-2) — the mock backend 404s it on purpose. */
const HAS_WS_PATH = (url: string) => url.includes('/ws/') || url.endsWith('/api/events/ws')

interface ConsoleCapture {
  errors: string[]
  pageErrors: string[]
  failedRequests: string[]
}

function attachConsoleCapture(page: import('@playwright/test').Page, cap: ConsoleCapture) {
  const onConsole = (msg: import('@playwright/test').ConsoleMessage) => {
    if (msg.type() !== 'error') return
    const text = msg.text()
    const url = (() => {
      try {
        return msg.location().url ?? ''
      } catch {
        return ''
      }
    })()
    const isResourceLoad = /Failed to load resource/i.test(text)
    // Documented filters: known-missing backend paths + the WS socket we open.
    if (isResourceLoad && KNOWN_MISSING_BACKEND_PATHS.some((p) => url.endsWith(p))) return
    if (text.includes('WebSocket') || HAS_WS_PATH(url)) return
    cap.errors.push(text)
  }
  const onPageError = (err: Error) => cap.pageErrors.push(err.message)
  const onReqFailed = (req: import('@playwright/test').Request) => {
    // The honest connection badge opens a real socket at /api/events/ws.
    // The mock backend has no ws route, so the failed upgrade is handled
    // in-state (badge → Reconnecting) and is not a request failure.
    if (HAS_WS_PATH(req.url())) return
    // Playwright/Chromium artifact (reproduced in zz-repro): a
    // `route.fulfill({ status: 204 })` is delivered to the page — the app
    // fetch resolves `status=204 ok=true` — but the network service still
    // cancels the pending stream, so `requestfailed` fires
    // `net::ERR_ABORTED` on the bodyless 204. GL-3 proves delivery
    // app-side (the row is removed only after the DELETE resolves), so
    // this network-layer abort is filtered exactly like the WS upgrade
    // above. Scoped to the goals DELETE so any other failure still
    // fails the test.
    if (
      req.method() === 'DELETE' &&
      req.url().includes('/api/goals') &&
      (req.failure()?.errorText ?? '').includes('ERR_ABORTED')
    )
      return
    cap.failedRequests.push(
      `${req.method()} ${req.url()} -> ${req.failure()?.errorText ?? 'unknown'}`,
    )
  }
  page.on('console', onConsole)
  page.on('pageerror', onPageError)
  page.on('requestfailed', onReqFailed)
  return () => {
    page.off('console', onConsole)
    page.off('pageerror', onPageError)
    page.off('requestfailed', onReqFailed)
  }
}

async function visitAuthenticated(page: import('@playwright/test').Page, path: string) {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'e2e-token')
  })
  await page.goto(path, { waitUntil: 'domcontentloaded' })
}

function assertConsoleClean(cap: ConsoleCapture) {
  expect(cap.pageErrors, 'uncaught exceptions').toEqual([])
  expect(cap.failedRequests, 'failed network requests').toEqual([])
  expect(cap.errors, 'console errors').toEqual([])
}

/** Fulfil an array list response. */
async function fulfillList(
  route: import('@playwright/test').Route,
  body: unknown,
  status = 200,
) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

test.describe('Overview (OV)', () => {
  test('renders live tiles + real goal/agent panels, no hardcoded Goal 1/2/3', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/goals', (r) =>
      fulfillList(r, [
        { id: 'goal-1', title: 'Make pipeline production ready', status: 'active', progress: 0.72, deadline: '2026-10-15' },
      ]),
    )
    await page.route('**/api/agents', (r) =>
      fulfillList(r, [{ id: 'agent-1', name: 'investigator-1', status: 'active', currentTask: 'Trace allocation stack' }]),
    )
    await page.route('**/api/tasks', (r) => fulfillList(r, []))
    await page.route('**/api/incidents', (r) => fulfillList(r, []))

    try {
      await visitAuthenticated(page, '/overview')

      // The four live stat tiles remain.
      for (const label of ['Active Goals', 'Active Agents', 'Pending Tasks', 'Active Incidents']) {
        await expect(page.getByText(label)).toBeVisible()
      }

      // Real data flows into the panels (not hardcoded placeholders).
      await expect(page.getByText('Make pipeline production ready')).toBeVisible()
      await expect(page.getByText('investigator-1')).toBeVisible()

      // The OV-2 hardcodes are gone.
      expect(await page.getByText('Goal 1', { exact: true }).count()).toBe(0)
      expect(await page.getByText('Goal 2', { exact: true }).count()).toBe(0)
      expect(await page.getByText('Goal 3', { exact: true }).count()).toBe(0)
      expect(await page.getByText('Status: In Progress', { exact: true }).count()).toBe(0)

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('shows alert banner with status — detail when an endpoint rejects', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/goals', (r) => fulfillList(r, []))
    await page.route('**/api/agents', (r) => fulfillList(r, []))
    await page.route('**/api/tasks', (r) =>
      fulfillList(r, { detail: 'tasks service error: missing column' }, 500),
    )
    await page.route('**/api/incidents', (r) => fulfillList(r, []))

    try {
      await visitAuthenticated(page, '/overview')

      const alert = page.getByRole('alert')
      await expect(alert).toBeVisible()
      const text = await alert.textContent()
      expect(text).toContain('500')
      expect(text).toContain('—')
      // OV-1: the failing tile is no longer a silent zero — the banner is shown.
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})

test.describe('Systems (SY)', () => {
  test('shows honest connection badge (text + aria-label), never a bare dot', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Canonical contract: GET /api/systems/ (trailing slash — the slashless
    // form 307s with an absolute Location that drops the host port).
    await page.route('**/api/systems/**', (r) =>
      fulfillList(r, [
        { name: 'nervecenter-api', status: 'healthy', version: '0.4.1', message: 'OK', checkedAt: '2026-10-08T00:00:00Z' },
        { name: 'lavalink', status: 'degraded', version: '4.2.1', message: 'monitoring', checkedAt: '2026-10-08T00:00:00Z' },
      ]),
    )

    try {
      await visitAuthenticated(page, '/systems')

      // Real system rows render from the endpoint.
      await expect(page.getByText('nervecenter-api')).toBeVisible()
      await expect(page.getByText('lavalink')).toBeVisible()

      // The connection badge is real: it carries visible text and an aria-label.
      const badge = page.getByRole('status', { name: /Real-time connection/i })
      await expect(badge).toBeVisible()
      const badgeText = (await badge.textContent()) ?? ''
      expect(badgeText).toContain('Real-time:')
      // Every state has a human label, not just a color.
      expect(
        /Connecting|Connected|Disconnected|Reconnecting/.test(badgeText),
      ).toBe(true)
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('surfaces the 501 detail in an alert banner (SY-3)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Same canonical URL as the SY-2 test: GET /api/systems/
    await page.route('**/api/systems/**', (r) =>
      fulfillList(r, { detail: 'Systems list endpoint requires the systems service to be wired.' }, 501),
    )

    try {
      await visitAuthenticated(page, '/systems')

      const alert = page.getByRole('alert')
      await expect(alert).toBeVisible()
      const text = (await alert.textContent()) ?? ''
      expect(text).toContain('501')
      expect(text).toContain('—')
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})

test.describe('Task DAG (TA)', () => {
  test('renders dependency graph + concurrency badges from real id/title/status fields', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Real backend shape: `id`, `title`, lowercase `status`, `dependencies`.
    await page.route('**/api/tasks', (r) =>
      fulfillList(r, [
        { id: 't-1', title: 'Collect evidence', status: 'completed', priority: 5, dependencies: [] },
        { id: 't-2', title: 'Trace allocation', status: 'in_progress', priority: 9, dependencies: ['t-1'] },
        { id: 't-3', title: 'Rank hypotheses', status: 'pending', priority: 9, dependencies: ['t-2'] },
      ]),
    )

    try {
      await visitAuthenticated(page, '/tasks')

      // TA-3: names come from real `title` fields.
      await expect(page.getByText('Collect evidence')).toBeVisible()
      await expect(page.getByText('Trace allocation')).toBeVisible()

      // TA-2: dependency graph — SVG present with one edge per dependency.
      const svg = page.getByTestId('task-dag-svg')
      await expect(svg).toBeVisible()
      // Edges are zero-height horizontal SVG curves: Playwright's
      // toBeVisible treats a zero-area bounding box as "hidden", so assert
      // existence + a real `d` path (the intent: one edge per dependency).
      await expect(page.getByTestId('task-edge-t-1-t-2')).toHaveCount(1)
      await expect(
        page.getByTestId('task-edge-t-1-t-2'),
      ).toHaveAttribute('d', /^M /)
      await expect(page.getByTestId('task-edge-t-2-t-3')).toHaveCount(1)
      await expect(
        page.getByTestId('task-edge-t-2-t-3'),
      ).toHaveAttribute('d', /^M /)

      // Nodes render with normalized uppercase status badges.
      await expect(page.getByTestId('task-node-t-2')).toContainText('RUNNING')
      await expect(page.getByTestId('task-node-t-1')).toContainText('COMPLETED')

      // Concurrency badges aggregate live state.
      await expect(page.getByRole('status', { name: /Concurrency/ })).toBeVisible()
      const badgeTexts = await page.locator('[role=status]').allTextContents()
      expect(badgeTexts.join(' ')).toMatch(/1 running/i)
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})

test.describe('Goals (GL)', () => {
  test('shows empty-state guidance and create flow posts + refreshes', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    const newGoal = {
      id: 'goal-new',
      title: 'Reduce latency below 1s p95',
      status: 'active',
      progress: 0.1,
      deadline: '2026-12-01',
      priority: 3,
    }
    let created = false

    await page.route('**/api/goals', async (route) => {
      if (route.request().method() === 'POST') {
        created = true
        await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(newGoal) })
      } else {
        await fulfillList(route, created ? [newGoal] : [])
      }
    })

    try {
      await visitAuthenticated(page, '/goals')

      // GL-2: empty state has guidance beyond a bare count.
      await expect(page.getByText(/No goals yet/)).toBeVisible()

      // GL-1: "+ New Goal" opens a real form.
      await page.getByRole('button', { name: /New Goal/i }).click()
      const form = page.getByRole('form', { name: /Create a new goal/i })
      await expect(form).toBeVisible()

      await page.getByLabel('Name').fill('Reduce latency below 1s p95')
      await page.getByLabel('Priority (0–10)').fill('3')
      await page.getByLabel('Due date').fill('2026-12-01')

      await page.getByRole('button', { name: /Create goal/i }).click()

      // The created goal renders with real progress/status/due-date.
      const card = page.getByTestId('goal-card-goal-new')
      await expect(card).toBeVisible()
      await expect(card).toContainText('Reduce latency below 1s p95')
      await expect(card).toContainText('10%') // progress 0.1 normalized to a percent
      await expect(card).toContainText('active')
      await expect(card).toContainText('2026') // due date rendered

      // The form closed after success.
      await expect(page.getByRole('form', { name: /Create a new goal/i })).toHaveCount(0)

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('GL-3: delete confirms inline, then DELETE /api/goals/{id} → 204 removes the row', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    const goal = {
      id: 'goal-1',
      title: 'Ship the observability plane',
      status: 'active',
      progress: 0.5,
      deadline: '2026-12-01',
      priority: 2,
    }
    let deletePath: string | null = null

    // Function predicate: the glob `**/api/goals*` cannot cross the `/`
    // boundary, so it would never intercept `DELETE /api/goals/{id}` — the
    // predicate below matches the list URL and every per-goal URL.
    await page.route((url: URL) => url.pathname.startsWith('/api/goals'), (route) => {
      if (route.request().method() === 'DELETE') {
        deletePath = route.request().url().split('/api/').pop() ?? null
        return route.fulfill({ status: 204, headers: { 'Content-Length': '0' } })
      }
      return fulfillList(route, [goal])
    })

    try {
      await visitAuthenticated(page, '/goals')

      const card = page.getByTestId('goal-card-goal-1')
      await expect(card).toBeVisible()

      // First tap: no request issued, inline confirmation shows the row identity.
      await page.getByTestId('goal-delete-goal-1').click()
      await expect(page.getByText('Delete “Ship the observability plane”?')).toBeVisible()
      expect(deletePath).toBeNull()

      // Cancel: confirmation dismisses, the goal remains, still no request.
      await page.getByTestId('goal-cancel-delete-goal-1').click()
      await expect(page.getByTestId('goal-delete-goal-1')).toBeVisible()
      await expect(card).toBeVisible()
      expect(deletePath).toBeNull()

      // Confirm: DELETE is issued and the row is removed locally.
      await page.getByTestId('goal-delete-goal-1').click()
      await page.getByTestId('goal-confirm-delete-goal-1').click()
      expect(deletePath).toBe('goals/goal-1')
      await expect(page.getByTestId('goal-card-goal-1')).toHaveCount(0)
      await expect(page.getByText(/No goals yet/)).toBeVisible()

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})
