import { expect, test, type Page } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * Agent live log (click a card), Markdown run output and the maximize view.
 * All agent traffic is mocked with page.route; the live endpoint returns the
 * events the test has "emitted" so far.
 */

const json = (body: unknown, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

const AGENT = {
  id: 'inst-1',
  name: 'bill-watcher',
  type: 'planner',
  status: 'active',
  description: 'Watches bills',
  runs_when: 'Your agent. It runs on the schedule you set.',
  prompt_template: null,
  tools: [],
  capabilities: null,
  lastHeartbeat: null,
  cpuUsage: null,
  memoryUsage: null,
  tasksCompleted: 1,
  tasksFailed: 0,
}

const REPORT = [
  '## Bills report',
  '',
  '| Bill | Due | Amount |',
  '| --- | --- | ---: |',
  '| Power | Nov 20 | $82.10 |',
  '',
  '- [x] Water paid',
  '- [ ] Internet due',
  '',
  'Run `nerve bills` for **details**.',
  '',
  '<script>window.__pwned = true</script>',
].join('\n')

interface LiveState {
  status: string
  events: Array<Record<string, unknown>>
}

async function mockAgents(page: Page, live: LiveState) {
  await page.route('**/api/agents/runner/status', (r) =>
    r.fulfill(json({ state: 'normal', stretch: { background: 1, time_critical: 1 }, throttles_in_window: 0, running: true, skillhub_configured: false })),
  )
  await page.route('**/api/agents/inst-1/schedule', (r) =>
    r.fulfill(json({ mode: 'on_demand', interval_seconds: null, daily_time: null, timezone: null, priority: 'background', enabled: false })),
  )
  await page.route('**/api/agents/inst-1/runs', (r) =>
    r.fulfill(
      json({
        data: [
          {
            id: 'run-1',
            agent_id: 'inst-1',
            status: 'completed',
            trigger: 'on_demand',
            started_at: '2026-11-17T01:00:00+00:00',
            finished_at: '2026-11-17T01:00:05+00:00',
            output: REPORT,
            error: null,
            latency_ms: 5000,
          },
        ],
      }),
    ),
  )
  await page.route('**/api/agents/inst-1/live**', (r) => {
    const after = Number(new URL(r.request().url()).searchParams.get('after') ?? '0')
    const run = { run_key: 1, started_at: '2026-11-17T01:00:00+00:00', finished_at: null, trigger: 'on_demand', status: live.status, events: live.events.length }
    return r.fulfill(
      json({
        running: live.status === 'running',
        run,
        runs: [run],
        events: live.events.filter((e) => (e.seq as number) > after),
        last_seq: live.events.length,
      }),
    )
  })
  await page.route('**/api/agents', (r) => r.fulfill(json({ data: [AGENT] })))
}

const ev = (seq: number, kind: string, text: string, data?: Record<string, unknown>) => ({
  seq,
  run_key: 1,
  at: '2026-11-17T01:00:00+00:00',
  kind,
  text,
  ...(data ? { data } : {}),
})

test('clicking an agent shows its live log as events arrive', async ({ page }) => {
  const live: LiveState = { status: 'running', events: [ev(1, 'run_started', 'Run started (on_demand)')] }
  await mockAgents(page, live)
  await seedBreakGlassSession(page)
  await page.goto('/agents', { waitUntil: 'domcontentloaded' })

  const card = page.locator('[data-agent-id="inst-1"]')
  await card.getByText('Watches bills').click()
  const log = card.getByTestId('agent-live-log')
  await expect(log).toBeVisible()
  await expect(log.getByTestId('agent-live-status')).toHaveText('running')
  await expect(log.getByText('Run started (on_demand)')).toBeVisible()

  live.events.push(
    ev(2, 'model_call', 'Model round 1: waiting for a reply', { round: 1 }),
    ev(3, 'tool_call', 'fetch.fetch', { server: 'fetch', tool: 'fetch', args: { url: 'https://example.org' } }),
    ev(4, 'model_reply', '**Found** 2 bills', { round: 2, output_tokens: 12, latency_ms: 900 }),
  )
  await expect(log.locator('[data-kind="tool_call"]')).toContainText('https://example.org')
  await expect(log.locator('[data-kind="model_reply"] strong')).toHaveText('Found')

  live.events.push(ev(5, 'run_finished', 'Completed in 900 ms', { status: 'completed' }))
  live.status = 'completed'
  await expect(log.getByTestId('agent-live-status')).toHaveText('completed', { timeout: 10_000 })
  await expect(log.getByTestId('agent-live-event')).toHaveCount(5)

  await log.getByTestId('agent-live-maximize').click()
  const overlay = page.getByTestId('maximize-overlay')
  await expect(overlay).toBeVisible()
  await expect(overlay.getByText('Completed in 900 ms')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(overlay).toHaveCount(0)

  // Clicking the card again hides the log.
  await card.getByText('Watches bills').click()
  await expect(card.getByTestId('agent-live-log')).toHaveCount(0)
})

test('completed run output renders Markdown and can be maximized', async ({ page }) => {
  await mockAgents(page, { status: 'completed', events: [] })
  await seedBreakGlassSession(page)
  await page.goto('/agents', { waitUntil: 'domcontentloaded' })

  const card = page.locator('[data-agent-id="inst-1"]')
  await card.getByTestId('agent-runner-toggle').click()
  const output = card.getByTestId('agent-run-output')
  await expect(output.getByRole('heading', { name: 'Bills report' })).toBeVisible()
  await expect(output.locator('table td', { hasText: '$82.10' })).toBeVisible()
  await expect(output.locator('input[type="checkbox"]')).toHaveCount(2)
  await expect(output.locator('code', { hasText: 'nerve bills' })).toBeVisible()
  await expect(output.locator('strong', { hasText: 'details' })).toBeVisible()
  // Raw HTML is not rendered.
  expect(await page.evaluate(() => (window as unknown as { __pwned?: boolean }).__pwned)).toBeUndefined()
  await expect(card.getByTestId('agent-live-log')).toHaveCount(0)

  await card.getByTestId('agent-run-maximize').click()
  const overlay = page.getByTestId('maximize-overlay')
  await expect(overlay.getByRole('heading', { name: 'Bills report' })).toBeVisible()
  await overlay.getByTestId('maximize-close').click()
  await expect(overlay).toHaveCount(0)
})
