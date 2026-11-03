import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * views-b.spec.ts — gap-closure round 2 (IN/DC/TL/INF/AG).
 *
 * Deterministic Playwright tests for the round-2 behavior added to
 * IncidentsView / DecisionsView / TimelineView / InferenceView / AgentsView.
 * All assertions are driven by `page.route` mocks so they never depend on
 * live backend state.
 *
 * Coverage (round 3 — IN-1 / AG-3 blockers resolved):
 *   IN-1  Incidents: "+ Report Incident" opens a real form; submitting POSTs
 *          to /api/incidents and surfaces the ApiError `status — detail` on
 *          non-2xx (round 3: real create route, not investigate).
 *   IN-2  Incidents: empty state carries guidance copy (not a bare count).
 *   DC-1  Decisions: a rejected fetch renders `role=alert` with
 *          `status — detail` instead of a silent empty list.
 *   TL-1  Timeline: real events render from GET /api/events; the old
 *          hardcoded samples are gone; a 503 bus failure surfaces in an
 *          alert banner; an empty bus shows honest guidance copy.
 *   INF-1 Inference: each empty section (nodes/routes/history) renders
 *          guidance copy.
 *   AG-2  Agents: status carries visible text + aria-label, never a bare dot.
 *   AG-3  Agents: "+ Add Agent" is enabled, opens a create form, submits
 *          POST /api/agents (201 → new row lists), and surfaces a 409
 *          duplicate-name detail verbatim on the error path.
 *
 * Console-error policy mirrors views-a.spec.ts: console error / pageerror /
 * requestfailed FAIL the test; the only tolerated resource-load errors are
 * the documented known-missing backend paths (none of these views hit one,
 * so the list is intentionally empty here).
 */

/**
 * Endpoints these tests deliberately fulfil with a non-2xx body to exercise
 * the error UI. The browser logs "Failed to load resource: ... 5xx/4xx" for
 * each, which is provably unrelated to app rendering (the view surfaces the
 * error in its UI banner, asserted below) — so they are filtered exactly as
 * views-a.spec.ts filters its known-missing backend paths.
 */
const KNOWN_MISSING_BACKEND_PATHS: string[] = [
  '/api/incidents', // IN-1 (round 3): create submit → 409 conflict
  '/api/decisions', // DC-1: list → 501
  '/api/events', // TL: bus → 503
  // Canonical endpoint since e59bc39: the client's `getEvents` issues
  // `GET /api/events/` (trailing slash — the slashless form 307s). The
  // resource-load URL ends with the slash, so filter that form too.
  '/api/events/',
  '/api/agents', // AG-3 (round 3): create submit → 409 duplicate name
]

interface ConsoleCapture {
  errors: string[]
  pageErrors: string[]
  failedRequests: string[]
}

function attachConsoleCapture(
  page: import('@playwright/test').Page,
  cap: ConsoleCapture,
) {
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
    if (isResourceLoad && KNOWN_MISSING_BACKEND_PATHS.some((p) => url.endsWith(p))) {
      return
    }
    if (text.includes('WebSocket') || url.includes('/ws/')) return
    cap.errors.push(text)
  }
  const onPageError = (err: Error) => cap.pageErrors.push(err.message)
  const onReqFailed = (req: import('@playwright/test').Request) => {
    if (req.url().includes('/ws/')) return
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

async function visitAuthenticated(
  page: import('@playwright/test').Page,
  path: string,
) {
  await seedBreakGlassSession(page)
  await page.goto(path, { waitUntil: 'domcontentloaded' })
}

function assertConsoleClean(cap: ConsoleCapture) {
  expect(cap.pageErrors, 'uncaught exceptions').toEqual([])
  expect(cap.failedRequests, 'failed network requests').toEqual([])
  expect(cap.errors, 'console errors').toEqual([])
}

/** Fulfil a raw JSON body (array or envelope). */
async function fulfillJson(
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

async function fulfillError(
  route: import('@playwright/test').Route,
  status: number,
  detail: string,
) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ detail }),
  })
}

test.describe('Incidents (IN)', () => {
  test('Report Incident opens a form, submits to POST /api/incidents, and surfaces the ApiError detail', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    let postBody: unknown = null
    await page.route('**/api/incidents', (r) =>
      fulfillJson(r, [
        {
          id: 'inc-1',
          title: 'GPU memory leak on esnixi',
          severity: 'high',
          status: 'active',
          createdAt: '2026-10-01T08:00:00Z',
          updatedAt: '2026-10-01T08:00:00Z',
          description: 'Leak reproduced in tensor cache.',
          impact: 'OOMKills on the node.',
          tags: ['gpu'],
        },
      ]),
    )
    // IN-1 (round 3): the form now posts to the real create route. A 409
    // conflict is the realistic failure for a duplicate title; the banner
    // must surface `status — detail` verbatim, not fake success.
    await page.route('**/api/incidents', async (r) => {
      if (r.request().method() === 'POST') {
        postBody = JSON.parse((await r.request().postData()) ?? 'null')
        await fulfillError(r, 409, 'Incident "esnixi" already exists')
        return
      }
      await fulfillJson(r, [
        {
          id: 'inc-1',
          title: 'GPU memory leak on esnixi',
          severity: 'high',
          status: 'active',
          createdAt: '2026-10-01T08:00:00Z',
          updatedAt: '2026-10-01T08:00:00Z',
          description: 'Leak reproduced in tensor cache.',
          impact: 'OOMKills on the node.',
          tags: ['gpu'],
        },
      ])
    })

    try {
      await visitAuthenticated(page, '/incidents')

      // Live row renders from the endpoint.
      await expect(page.getByText('GPU memory leak on esnixi')).toBeVisible()

      // IN-1: button opens the real form.
      await page.getByRole('button', { name: /\+ Report Incident/ }).click()
      const form = page.getByTestId('incident-form')
      await expect(form).toBeVisible()

      await form.getByTestId('incident-form-title').fill('esnixi')
      await form.getByTestId('incident-form-severity').selectOption('critical')
      await form.getByTestId('incident-form-description').fill('Repro steps attached.')

      await form.getByRole('button', { name: /Submit report/ }).click()

      // The 409 is surfaced verbatim — `status — detail` — not faked as success.
      // (Waiting for the alert also guarantees the POST round-trip has
      // completed, since the banner only renders after the response arrives.)
      const alert = form.getByRole('alert')
      await expect(alert).toBeVisible()

      // The POST actually went out to the create route with the reported
      // title + severity.
      expect(postBody).not.toBeNull()
      expect((postBody as { title?: string }).title).toBe('esnixi')
      expect((postBody as { severity?: string }).severity).toBe('critical')
      const text = (await alert.textContent()) ?? ''
      expect(text).toContain('409')
      expect(text).toContain('—')
      expect(text).toContain('Incident "esnixi" already exists')

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('empty state carries guidance copy (IN-2)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/incidents', (r) => fulfillJson(r, []))

    try {
      await visitAuthenticated(page, '/incidents')

      const empty = page.getByTestId('incidents-empty')
      await expect(empty).toBeVisible()
      const text = (await empty.textContent()) ?? ''
      expect(text).toContain('No incidents recorded')
      // Guidance copy, not a bare count.
      expect(text).toContain('Report Incident')
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})

test.describe('Decisions (DC)', () => {
  test('rejected fetch shows role=alert `status — detail`, not a silent empty list (DC-1)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/decisions', (r) =>
      fulfillError(r, 501, 'Decision listing is not implemented on this backend'),
    )

    try {
      await visitAuthenticated(page, '/decisions')

      const alert = page.getByRole('alert')
      await expect(alert).toBeVisible()
      const text = (await alert.textContent()) ?? ''
      expect(text).toContain('501')
      expect(text).toContain('—')
      expect(text).toContain('not implemented')

      // The silent empty-list state must NOT be shown on error.
      await expect(page.getByTestId('decisions-empty')).toHaveCount(0)

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('genuine no-data still renders the honest empty state', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/decisions', (r) => fulfillJson(r, []))

    try {
      await visitAuthenticated(page, '/decisions')

      await expect(page.getByTestId('decisions-empty')).toBeVisible()
      await expect(page.getByRole('alert')).toHaveCount(0)
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})

test.describe('Timeline (TL)', () => {
  test('renders real events from GET /api/events; hardcoded samples are gone (TL-1)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Canonical contract: GET /api/events/ (trailing-slash form — the
    // slashless form 307-redirects with an absolute Location that breaks in
    // the browser, so the client always uses /api/events/).
    await page.route('**/api/events/**', (r) =>
      fulfillJson(r, {
        data: [
          {
            id: 'ev-1',
            event_type: 'task_state_change',
            category: 'task',
            occurred_at: '2026-10-08T05:00:00Z',
            subject: 't-042',
            payload: { message: 'Task t-042 entered VERIFYING' },
            severity: 'info',
          },
        ],
        meta: { pagination: { total: 1, page: 1, per_page: 1 } },
      }),
    )

    try {
      await visitAuthenticated(page, '/timeline')

      // Real event content from the API.
      await expect(page.getByText('Task t-042 entered VERIFYING')).toBeVisible()
      await expect(page.getByText('task_state_change')).toBeVisible()

      // The round-1 hardcoded fixtures must be gone.
      await expect(page.getByText('Agent "data-processor" started successfully')).toHaveCount(0)
      await expect(page.getByText('Task "data-sync" completed in 45s')).toHaveCount(0)

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('bus 503 surfaces as an alert banner', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Same canonical URL as the TL-1 test: GET /api/events/
    await page.route('**/api/events/**', (r) =>
      fulfillError(r, 503, 'event bus unreachable: stream not found'),
    )

    try {
      await visitAuthenticated(page, '/timeline')

      const alert = page.getByRole('alert')
      await expect(alert).toBeVisible()
      const text = (await alert.textContent()) ?? ''
      expect(text).toContain('503')
      expect(text).toContain('—')
      expect(text).toContain('event bus unreachable')
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('empty bus shows honest guidance copy', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // Same canonical URL: GET /api/events/
    await page.route('**/api/events/**', (r) => fulfillJson(r, { data: [], meta: {} }))

    try {
      await visitAuthenticated(page, '/timeline')

      const empty = page.getByTestId('timeline-empty')
      await expect(empty).toBeVisible()
      const text = (await empty.textContent()) ?? ''
      expect(text).toContain('No events recorded')
      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})

test.describe('Inference (INF)', () => {
  test('each empty section renders guidance copy (INF-1)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/compute', (r) => fulfillJson(r, []))
    await page.route('**/api/routes', (r) => fulfillJson(r, []))
    await page.route('**/api/models/history', (r) => fulfillJson(r, []))

    try {
      await visitAuthenticated(page, '/inference')

      const nodes = page.getByTestId('inference-empty-nodes')
      const routes = page.getByTestId('inference-empty-routes')
      const history = page.getByTestId('inference-empty-history')

      await expect(nodes).toBeVisible()
      await expect(routes).toBeVisible()
      await expect(history).toBeVisible()

      expect((await nodes.textContent()) ?? '').toContain('No compute nodes')
      expect((await routes.textContent()) ?? '').toContain('No OmniRoute routes')
      expect((await history.textContent()) ?? '').toContain('No model history')

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})

test.describe('Agents (AG)', () => {
  test('status carries text + aria-label, never a bare dot (AG-2)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/agents', (r) =>
      fulfillJson(r, [
        {
          id: 'agent-1',
          name: 'investigator-1',
          type: 'investigator',
          status: 'active',
          lastHeartbeat: '2026-10-08T05:55:00Z',
          cpuUsage: 12,
          memoryUsage: 34,
          tasksCompleted: 120,
          tasksFailed: 3,
        },
        {
          id: 'agent-2',
          name: 'deployer-1',
          type: 'operator',
          status: 'paused',
          lastHeartbeat: '2026-10-08T04:00:00Z',
          cpuUsage: 0,
          memoryUsage: 8,
          tasksCompleted: 40,
          tasksFailed: 0,
        },
      ]),
    )

    try {
      await visitAuthenticated(page, '/agents')

      await expect(page.getByText('investigator-1')).toBeVisible()
      await expect(page.getByText('deployer-1')).toBeVisible()

      // Every status has visible text…
      await expect(page.getByText('active', { exact: true })).toBeVisible()
      await expect(page.getByText('paused', { exact: true })).toBeVisible()

      // …and an aria-label naming the agent + status.
      const badges = page.getByRole('status', { name: /status:/i })
      expect(await badges.count()).toBe(2)
      expect(await badges.first().getAttribute('aria-label')).toContain('investigator-1')
      expect(await badges.first().getAttribute('aria-label')).toContain('active')

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('"+ Add Agent" opens a create form, submits to POST /api/agents (201), and lists the new agent (AG-3)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    // The list route must echo the freshly created agent so the view can
    // render it after the 201. (Round 3: AG-3 resolved — the create route
    // exists, so the button is enabled and the flow is real.)
    const createdAgent = {
      id: 'ag-1',
      name: 'scout-1',
      type: 'planner',
      status: 'active',
      description: null,
      prompt_template: null,
      tools: [],
      lastHeartbeat: null,
      cpuUsage: null,
      memoryUsage: null,
      tasksCompleted: null,
      tasksFailed: null,
    }
    await page.route('**/api/agents', (r) =>
      fulfillJson(r, [createdAgent]),
    )

    try {
      await visitAuthenticated(page, '/agents')

      const button = page.getByRole('button', { name: /\+ Add Agent/ })
      await expect(button).toBeVisible()
      await expect(button).toBeEnabled()

      await button.click()
      const form = page.getByTestId('agent-form')
      await expect(form).toBeVisible()

      await form.getByTestId('agent-form-name').fill('scout-1')
      await form.getByTestId('agent-form-type').selectOption('planner')
      await form.getByRole('button', { name: /Create agent/ }).click()

      // The new agent surfaces in the list (echoed by the list route).
      await expect(page.getByText('scout-1')).toBeVisible()

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })

  test('duplicate agent name surfaces the 409 detail verbatim (AG-3 error path)', async ({ page }) => {
    const cap: ConsoleCapture = { errors: [], pageErrors: [], failedRequests: [] }
    const detach = attachConsoleCapture(page, cap)

    await page.route('**/api/agents', (r) => {
      if (r.request().method() === 'POST') {
        return fulfillError(r, 409, "An agent named 'scout-1' already exists.")
      }
      return fulfillJson(r, [])
    })

    try {
      await visitAuthenticated(page, '/agents')

      await page.getByRole('button', { name: /\+ Add Agent/ }).click()
      const form = page.getByTestId('agent-form')
      await expect(form).toBeVisible()

      await form.getByTestId('agent-form-name').fill('scout-1')
      await form.getByTestId('agent-form-type').selectOption('planner')
      await form.getByRole('button', { name: /Create agent/ }).click()

      const alert = form.getByRole('alert')
      await expect(alert).toBeVisible()
      const text = (await alert.textContent()) ?? ''
      expect(text).toContain('409')
      expect(text).toContain('—')
      expect(text).toContain("An agent named 'scout-1' already exists.")

      assertConsoleClean(cap)
    } finally {
      detach()
    }
  })
})
