import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * agent-controls.spec.ts — NS-ADR-116: built-in agents get Run now, Schedule &
 * runs, Edit (an override on top of the code manifest) and Reset to defaults;
 * agents the owner created keep Edit and Delete. Driven by page.route mocks.
 */
const json = (body: unknown, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

const base = {
  status: 'active',
  tools: [],
  capabilities: null,
  lastHeartbeat: null,
  cpuUsage: null,
  memoryUsage: null,
  tasksCompleted: 0,
  tasksFailed: 0,
  skills: [] as string[],
}

test('built-in agents can run, schedule, edit and reset; custom agents delete', async ({ page }) => {
  let planner: Record<string, unknown> = {
    ...base,
    id: 'planner',
    name: 'Planner',
    type: 'planner',
    builtin: true,
    has_override: false,
    prompt_addition: '',
    description: 'Breaks goals into task plans',
    runs_when: 'Built-in. The system invokes it when a goal needs a plan.',
    prompt_template: 'You are a planning agent.',
  }
  let custom: Record<string, unknown> | null = {
    ...base,
    id: 'inst-1',
    name: 'bill-watcher',
    type: 'planner',
    builtin: false,
    description: 'Watches bills',
    runs_when: 'Your agent.',
    prompt_template: null,
  }
  const calls: string[] = []
  let patchBody: Record<string, unknown> | null = null
  let schedule: Record<string, unknown> = {
    mode: 'on_demand', interval_seconds: null, daily_time: null, timezone: null, priority: 'background', enabled: false,
  }
  let runs: unknown[] = []

  await page.route('**/api/agents/runner/status', (r) =>
    r.fulfill(json({ state: 'normal', stretch: { background: 1, time_critical: 1 }, throttles_in_window: 0, running: true, skillhub_configured: true })),
  )
  await page.route('**/api/agents/planner/run', (r) => {
    calls.push('run')
    const run = { id: 'r1', agent_id: 'planner', started_at: '2026-01-01T12:00:00+00:00', finished_at: null, status: 'completed', trigger: 'on_demand', output: 'plan ready', error: null, input_tokens: 1, output_tokens: 1, latency_ms: 40 }
    runs = [run]
    return r.fulfill(json(run, 201))
  })
  await page.route('**/api/agents/planner/runs', (r) => r.fulfill(json({ data: runs })))
  await page.route('**/api/agents/planner/schedule', (r) => {
    if (r.request().method() === 'PUT') {
      calls.push('schedule')
      schedule = { ...r.request().postDataJSON(), next_run_at: '2026-01-01T13:00:00+00:00' }
    }
    return r.fulfill(json(schedule))
  })
  await page.route('**/api/agents/planner/reset', (r) => {
    calls.push('reset')
    planner = { ...planner, has_override: false, prompt_addition: '', status: 'active', skills: [] }
    schedule = { ...schedule, mode: 'on_demand', enabled: false }
    return r.fulfill(json(planner))
  })
  await page.route(/\/(api\/)?agents\/planner$/, (r) => {
    const method = r.request().method()
    if (method === 'PATCH') {
      patchBody = r.request().postDataJSON()
      planner = {
        ...planner,
        has_override: true,
        prompt_addition: patchBody?.prompt_addition ?? planner.prompt_addition,
        skills: patchBody?.skills ?? planner.skills,
        status: patchBody?.enabled === false ? 'disabled' : 'active',
      }
    }
    if (method === 'DELETE') return r.fulfill(json({ detail: 'no' }, 405))
    return r.fulfill(json(planner))
  })
  await page.route('**/api/agents/inst-1', (r) => {
    if (r.request().method() === 'DELETE') {
      custom = null
      return r.fulfill({ status: 204 })
    }
    return r.fulfill(json(custom))
  })
  await page.route('**/api/agents', (r) => r.fulfill(json({ data: custom ? [planner, custom] : [planner] })))
  page.on('dialog', (d) => void d.accept())

  await seedBreakGlassSession(page)
  await page.goto('/agents', { waitUntil: 'domcontentloaded' })

  const card = page.locator('[data-agent-id="planner"]')
  await expect(card.getByTestId('agent-builtin-badge')).toContainText('Built-in')
  await expect(card.getByText('Breaks goals into task plans')).toBeVisible()
  await expect(card.getByTestId('agent-delete')).toHaveCount(0)
  await expect(card.getByTestId('agent-edit')).toBeVisible()
  await expect(card.getByTestId('agent-runner-toggle')).toBeVisible()
  await expect(card.getByTestId('agent-reset')).toBeVisible()

  // Run now from the card shows the recorded run.
  await card.getByTestId('agent-card-run').click()
  await expect(card.getByTestId('agent-run-history')).toContainText('plan ready')

  // Schedule.
  await card.getByTestId('schedule-mode').selectOption('interval')
  await card.getByTestId('schedule-minutes').fill('30')
  await card.getByRole('button', { name: 'Save schedule' }).click()
  await expect(card.getByText('Schedule saved.')).toBeVisible()
  expect(calls).toContain('schedule')

  // Edit: only the override fields are offered and sent.
  await card.getByTestId('agent-edit').click()
  const form = page.locator('form[data-testid="agent-edit"]')
  await expect(form.getByTestId('agent-edit-builtin')).toBeVisible()
  await expect(form.getByTestId('agent-edit-name')).toHaveCount(0)
  await form.getByTestId('agent-edit-addition').fill('List risks first.')
  await form.getByTestId('agent-edit-skills').fill('imported:ops/runbooks')
  await form.getByTestId('agent-edit-enabled').uncheck()
  await form.getByTestId('agent-edit-save').click()
  await expect(page.getByTestId('agent-edit-success')).toBeVisible()
  expect(patchBody).toEqual({
    prompt_addition: 'List risks first.',
    skills: ['imported:ops/runbooks'],
    enabled: false,
  })
  await expect(card.getByTestId('agent-builtin-badge')).toContainText('edited')

  // Reset to defaults.
  await card.getByTestId('agent-reset').click()
  await expect.poll(() => calls.includes('reset')).toBe(true)
  await expect(card.getByTestId('agent-builtin-badge')).not.toContainText('edited')

  // A custom agent keeps Delete.
  const own = page.locator('[data-agent-id="inst-1"]')
  await expect(own.getByTestId('agent-reset')).toHaveCount(0)
  await own.getByTestId('agent-delete').click()
  await expect(page.locator('[data-agent-id="inst-1"]')).toHaveCount(0)
})
