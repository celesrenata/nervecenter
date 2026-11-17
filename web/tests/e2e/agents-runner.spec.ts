import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * agents-runner.spec.ts — FEAT-001: delete, "what it does" text, schedule
 * editor, run history and the back-off banner. All driven by page.route mocks.
 */
const json = (body: unknown, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

const OWN = {
  id: 'inst-1',
  name: 'bill-watcher',
  type: 'planner',
  status: 'active',
  description: 'Watches bills',
  runs_when: 'Your agent. It runs on the schedule you set.',
  prompt_template: null,
  tools: ['read_file'],
  capabilities: null,
  lastHeartbeat: null,
  cpuUsage: null,
  memoryUsage: null,
  tasksCompleted: 2,
  tasksFailed: 1,
}
const BUILTIN = { ...OWN, id: 'planner', name: 'Planner', runs_when: 'Built-in. Invoked by the system.' }

test('agents: explain, schedule, run now, history, delete, back-off banner', async ({ page }) => {
  let agents = [BUILTIN, OWN]
  let runs: unknown[] = []
  await page.route('**/api/agents/runner/status', (r) =>
    r.fulfill(
      json({ state: 'degraded', stretch: { background: 2, time_critical: 1 }, throttles_in_window: 1, running: true, skillhub_configured: false }),
    ),
  )
  await page.route('**/api/agents/inst-1/schedule', (r) =>
    r.request().method() === 'PUT'
      ? r.fulfill(json({ ...r.request().postDataJSON(), next_run_at: '2026-01-01T13:00:00+00:00' }))
      : r.fulfill(json({ mode: 'on_demand', interval_seconds: null, daily_time: null, timezone: null, priority: 'background', enabled: false })),
  )
  await page.route('**/api/agents/inst-1/runs', (r) => r.fulfill(json({ data: runs })))
  await page.route('**/api/agents/inst-1/run', (r) => {
    const run = { id: 'r1', agent_id: 'inst-1', started_at: '2026-01-01T12:00:00+00:00', finished_at: null, status: 'completed', trigger: 'on_demand', output: 'all quiet', error: null, input_tokens: 1, output_tokens: 1, latency_ms: 40 }
    runs = [run]
    return r.fulfill(json(run, 201))
  })
  await page.route('**/api/agents/inst-1', (r) => {
    if (r.request().method() === 'DELETE') {
      agents = [BUILTIN]
      return r.fulfill({ status: 204 })
    }
    return r.fulfill(json(OWN))
  })
  await page.route('**/api/agents', (r) => r.fulfill(json({ data: agents })))
  page.on('dialog', (d) => void d.accept())

  await seedBreakGlassSession(page)
  await page.goto('/agents', { waitUntil: 'domcontentloaded' })

  await expect(page.getByTestId('runner-backoff-banner')).toBeVisible()
  await expect(page.getByText('Watches bills').first()).toBeVisible()
  await expect(page.getByText('Built-in. Invoked by the system.')).toBeVisible()
  // Built-ins have no Delete control (they get Reset to defaults instead).
  await expect(page.getByTestId('agent-delete')).toHaveCount(1)

  const own = page.locator('[data-agent-id="inst-1"]')
  // Wait for the panel's initial schedule load, or it overwrites the cadence picked below.
  const scheduleLoaded = Promise.all([
    page.waitForResponse((r) => r.url().includes('/api/agents/inst-1/schedule') && r.request().method() === 'GET'),
    page.waitForResponse((r) => r.url().endsWith('/api/agents/inst-1/runs')),
  ])
  await own.getByTestId('agent-runner-toggle').click()
  await scheduleLoaded
  await page.getByTestId('schedule-mode').selectOption('interval')
  await page.getByTestId('schedule-minutes').fill('30')
  await page.getByRole('button', { name: 'Save schedule' }).click()
  await expect(page.getByText('Schedule saved.')).toBeVisible()

  await page.getByTestId('agent-run-now').click()
  await expect(page.getByTestId('agent-run-history')).toContainText('all quiet')
  await expect(page.getByTestId('agent-run-history')).toContainText('1 completed')

  await own.getByTestId('agent-delete').click()
  await expect(page.getByText('bill-watcher')).toHaveCount(0)
})
