import { expect, test, type Page } from '@playwright/test'

/**
 * AC-WEBUI (web scope) — per-request inference + agent control.
 *
 * INFER_CONTROL_CONTRACT.md §2 (web scope): a request body may carry an
 * optional `inference: {provider?, model?}` (top level) and `agent` (top level).
 *
 * These tests assert the CLIENT's wire shape. The inference endpoints are
 * mocked so the tests are backend-independent and deterministic — the web
 * worktree does not necessarily expose `GET /api/inference/available` yet
 * (a Batch-4 backend concern), and the current `AskRequest` rejects unknown
 * top-level fields, so asserting the exact body the UI sends (not backend
 * acceptance) is the correct web-scope check. Auth still runs against the
 * real backend.
 *
 * The picker is collapsed by default, so a plain /ask or /inference load
 * issues no extra requests (keeps the views.spec.ts sweep green).
 */

const AVAILABLE_FIXTURE = {
  default: { provider: 'omniroute', model: 'nerve-ask' },
  providers: [
    {
      provider: 'omniroute',
      model_id: 'nerve-ask',
      base_url: null,
      key_configured: true,
      capabilities: [],
      default: true,
    },
  ],
  models: ['nerve-ask', 'nerve-deep'],
}

/**
 * Seed a real access token the same way views.spec.ts does: mint one through
 * the break-glass login (enabled in global-setup.mjs) and write it to the
 * sessionStorage key the auth transport actually reads (src/api/client.ts:
 * `nc.accessToken`). A stale/wrong key would make every auth-gated route 401.
 */
async function seedAuth(page: Page) {
  const login = await fetch('http://127.0.0.1:8000/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: 'local-admin',
      password: 'e2e-breakglass',
    }),
  })
  if (!login.ok) {
    throw new Error(`seedAuth: break-glass login failed (HTTP ${login.status})`)
  }
  const { access_token } = (await login.json()) as { access_token: string }
  await page.addInitScript((token: string) => {
    sessionStorage.setItem('nc.accessToken', token)
  }, access_token)
}

/**
 * Collect uncaught page errors (real render/runtime bugs). Returns a cleanup
 * + assertion pair; console 4xx/5xx from unrelated sections is intentionally
 * NOT failed here (the views.spec.ts sweep owns that policy).
 */
function trackPageErrors(page: Page) {
  const errors: string[] = []
  const on = (err: Error) => errors.push(err.message)
  page.on('pageerror', on)
  return {
    dispose: () => page.off('pageerror', on),
    assertClean: () => {
      expect(errors, `uncaught page errors: ${errors.join(' | ')}`).toEqual([])
    },
  }
}

/** Deterministic GET /api/inference/available fixture. */
function mockAvailable(page: Page) {
  return page.route('**/api/inference/available', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      // Contract §1 envelope — the client's getAvailable() reads
      // `body.result`, mirroring the real GET /api/inference/available.
      body: JSON.stringify({
        accepted: true,
        executed: true,
        service: 'inference',
        request_id: 'mock-available',
        timestamp: '2026-11-02T00:00:00Z',
        result: AVAILABLE_FIXTURE,
      }),
    }),
  )
}

/**
 * Deterministic GET /api/agents fixture. Returns a bare array so
 * `agentsApi.list()` (which accepts `unknown[] | {data: unknown[]}`) parses it.
 */
function mockAgents(
  page: Page,
  inference?: { provider?: string | null; model?: string | null } | null,
) {
  const agent: Record<string, unknown> = {
    id: 'ag-1',
    name: 'Research',
    type: 'persistent',
    status: 'ready',
    description: null,
    prompt_template: null,
    tools: [],
    capabilities: null,
    lastHeartbeat: null,
    cpuUsage: null,
    memoryUsage: null,
    tasksCompleted: 0,
    tasksFailed: 0,
  }
  if (inference !== undefined) agent.inference = inference
  return page.route('**/api/agents', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([agent]),
    }),
  )
}

test('Ask picker sends the selected inference + agent on the wire', async ({
  page,
}) => {
  const guard = trackPageErrors(page)
  await mockAvailable(page)
  await mockAgents(page)

  let captured: Record<string, unknown> | null = null
  await page.route('**/api/context/ask', (route) => {
    // postDataJSON() already returns a parsed object (or null).
    captured = route.request().postDataJSON() as Record<string, unknown> | null
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        accepted: true,
        executed: false,
        service: 'context',
        status: 'unavailable',
        request_status: 'pending',
        reason: 'no inference transport in mock mode',
      }),
    })
  })

  await seedAuth(page)
  await page.goto('/ask', { waitUntil: 'domcontentloaded' })

  // Collapsed by default — the panel is NOT rendered, so no options request
  // fires on a plain load (keeps the views.spec.ts sweep green).
  await expect(
    page.locator('[data-testid="ask-advanced-toggle"]'),
  ).toBeVisible()
  await expect(page.locator('[data-testid="ask-advanced-panel"]')).toHaveCount(
    0,
  )

  // Expand → the lazy options fetch runs and populates the selects.
  await page.locator('[data-testid="ask-advanced-toggle"]').click()
  await expect(page.locator('[data-testid="ask-advanced-panel"]')).toBeVisible()
  await expect(
    page.locator('#ask-model-select option[value="nerve-deep"]'),
  ).toHaveCount(1)
  await expect(page.locator('#ask-agent-select option[value="ag-1"]')).toHaveCount(
    1,
  )

  // Choose a non-default model + an agent, then submit.
  await page.locator('#ask-model-select').selectOption('nerve-deep')
  await page.locator('#ask-agent-select').selectOption('ag-1')
  await page.getByPlaceholder('Type your message...').fill('Route this one.')
  await page.getByRole('button', { name: 'Send' }).click()

  // Wire shape: the override rides on the request body.
  expect(captured).not.toBeNull()
  expect(captured!.question).toBe('Route this one.')
  expect(captured!.inference).toEqual({ model: 'nerve-deep' })
  expect(captured!.agent).toBe('ag-1')

  guard.dispose()
  guard.assertClean()
})

test('Ask default path sends a bare {question} body (no override keys)', async ({
  page,
}) => {
  const guard = trackPageErrors(page)
  let captured: Record<string, unknown> | null = null
  await page.route('**/api/context/ask', (route) => {
    // postDataJSON() already returns a parsed object (or null).
    captured = route.request().postDataJSON() as Record<string, unknown> | null
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        accepted: true,
        executed: false,
        service: 'context',
        status: 'unavailable',
        request_status: 'pending',
        reason: 'x',
      }),
    })
  })

  await seedAuth(page)
  await page.goto('/ask', { waitUntil: 'domcontentloaded' })

  // No picker expansion, no selection — the body must stay the legacy shape.
  await page.getByPlaceholder('Type your message...').fill('hello world')
  await page.getByRole('button', { name: 'Send' }).click()

  expect(captured).toEqual({ question: 'hello world' })

  guard.dispose()
  guard.assertClean()
})

test('Inference view exposes the available models + default on demand', async ({
  page,
}) => {
  const guard = trackPageErrors(page)
  await mockAvailable(page)

  await seedAuth(page)
  await page.goto('/inference', { waitUntil: 'domcontentloaded' })

  // Collapsed by default — panel not rendered, no request fired on load.
  await expect(
    page.locator('[data-testid="inference-available-toggle"]'),
  ).toBeVisible()
  await expect(
    page.locator('[data-testid="inference-available-panel"]'),
  ).toHaveCount(0)

  // Expand → default + providers + models render from the mocked response.
  await page.locator('[data-testid="inference-available-toggle"]').click()
  await expect(
    page.locator('[data-testid="inference-available-panel"]'),
  ).toBeVisible()
  await expect(
    page.locator('[data-testid="inference-available-default"]'),
  ).toHaveText(/omniroute \/ nerve-ask/)
  await expect(
    page.locator('[data-testid="inference-available-provider"]'),
  ).toHaveCount(1)
  await expect(
    page
      .locator('[data-testid="inference-available-model"]')
      .filter({ hasText: 'nerve-deep' }),
  ).toHaveCount(1)

  guard.dispose()
  guard.assertClean()
})

test('Agents view persists an inference binding via PATCH', async ({ page }) => {
  const guard = trackPageErrors(page)
  await mockAvailable(page)
  await mockAgents(page)

  let patchBody: Record<string, unknown> | null = null
  // Register the PATCH route LAST so (Playwright LIFO route matching) it is
  // checked before the `**/api/agents` GET mock; it only handles the PATCH
  // and falls through otherwise.
  await page.route('**/api/agents/ag-1', (route) => {
    if (route.request().method() === 'PATCH') {
      // postDataJSON() already returns a parsed object (or null).
      patchBody = route.request().postDataJSON() as Record<string, unknown> | null
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'ag-1',
          name: 'Research',
          type: 'persistent',
          status: 'ready',
          inference: { provider: 'omniroute', model: 'nerve-deep' },
        }),
      })
    }
    return route.fallback()
  })

  await seedAuth(page)
  await page.goto('/agents', { waitUntil: 'domcontentloaded' })

  // The persisted agent shows its inference badge + editor entry point.
  await expect(page.locator('[data-testid="agent-inference-badge"]')).toHaveText(
    /default/,
  )
  await page.locator('[data-testid="agent-inference"]').click()

  // The editor opened; select provider + model and save.
  await page.locator('#agent-infer-provider').selectOption('omniroute')
  await page.locator('#agent-infer-model').selectOption('nerve-deep')
  await page.locator('[data-testid="agent-infer-save"]').click()

  // Wire shape: a partial PATCH carrying only the binding.
  expect(patchBody).toEqual({
    inference: { provider: 'omniroute', model: 'nerve-deep' },
  })
  await expect(page.locator('[data-testid="agent-infer-success"]')).toBeVisible()

  guard.dispose()
  guard.assertClean()
})
