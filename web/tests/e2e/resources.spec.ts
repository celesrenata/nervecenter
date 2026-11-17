import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Resources page and the agent link multi-select (NS-ADR-125), driven by page.route mocks. */
const json = (body: unknown, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})
const mk = (id: string, name: string, sensitivity: 'normal' | 'secret') => ({
  id, name, content_type: 'text/plain', size: 12, sha256: 'a'.repeat(64), sensitivity,
  uploaded_by: 'owner', created_at: '2026-01-01T00:00:00+00:00',
})

test('upload, list with sensitivity badge, delete', async ({ page }) => {
  let rows = [mk('r1', 'notes.md', 'normal'), mk('r2', 'firewall.xml', 'secret')]
  const uploads: string[] = []
  await page.route(/\/api\/resources(\?|$|\/)/, (r) => {
    const req = r.request()
    const url = new URL(req.url())
    if (req.method() === 'PUT') {
      uploads.push(`${url.searchParams.get('name')}|${url.searchParams.get('sensitivity')}`)
      const row = mk('r3', url.searchParams.get('name') ?? 'x', url.searchParams.get('sensitivity') as 'normal' | 'secret')
      rows = [...rows, row]
      return r.fulfill(json(row, 201))
    }
    if (req.method() === 'DELETE') {
      rows = rows.filter((x) => !url.pathname.endsWith(`/${x.id}`))
      return r.fulfill({ status: 204 })
    }
    return r.fulfill(json({ resources: rows }))
  })
  page.on('dialog', (d) => void d.accept())
  await seedBreakGlassSession(page)
  await page.goto('/resources', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Resources' })).toBeVisible()
  await expect(page.getByTestId('resource-badge-r1')).toHaveText('normal')
  await expect(page.getByTestId('resource-badge-r2')).toHaveText('secret')

  await page.getByTestId('resource-file').setInputFiles({ name: 'plan.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') })
  await page.getByTestId('resource-sensitivity').selectOption('secret')
  await page.getByTestId('resource-submit').click()
  await expect(page.getByTestId('resource-badge-r3')).toHaveText('secret')
  expect(uploads).toEqual(['plan.txt|secret'])

  await page.getByTestId('resource-delete-r1').click()
  await expect(page.getByTestId('resource-r1')).toHaveCount(0)
})

test('agent edit panel links only normal resources', async ({ page }) => {
  let linked: string[] = ['r1']
  let putBody: { resource_ids: string[] } | null = null
  const planner = {
    status: 'active', tools: [], capabilities: null, lastHeartbeat: null, cpuUsage: null, memoryUsage: null,
    tasksCompleted: 0, tasksFailed: 0, skills: [] as string[], id: 'planner', name: 'Planner', type: 'planner',
    builtin: true, has_override: false, prompt_addition: '', description: 'Plans', runs_when: 'Built-in.',
    prompt_template: 'You are a planning agent.',
  }
  const all = [mk('r1', 'notes.md', 'normal'), mk('r2', 'runbook.md', 'normal'), mk('r3', 'firewall.xml', 'secret')]
  await page.route('**/api/agents/runner/status', (r) =>
    r.fulfill(json({ state: 'normal', stretch: { background: 1, time_critical: 1 }, throttles_in_window: 0, running: true, skillhub_configured: true })),
  )
  await page.route('**/api/agents/planner/runs', (r) => r.fulfill(json({ data: [] })))
  await page.route('**/api/agents/planner/schedule', (r) =>
    r.fulfill(json({ mode: 'on_demand', interval_seconds: null, daily_time: null, timezone: null, priority: 'background', enabled: false })),
  )
  await page.route('**/api/agents/planner/resources', (r) => {
    if (r.request().method() === 'PUT') {
      putBody = r.request().postDataJSON()
      linked = putBody?.resource_ids ?? []
    }
    return r.fulfill(json({ resources: all.filter((x) => linked.includes(x.id)) }))
  })
  await page.route(/\/api\/resources$/, (r) => r.fulfill(json({ resources: all })))
  await page.route(/\/(api\/)?agents\/planner$/, (r) => r.fulfill(json(planner)))
  await page.route('**/api/agents', (r) => r.fulfill(json({ data: [planner] })))
  await seedBreakGlassSession(page)
  await page.goto('/agents', { waitUntil: 'domcontentloaded' })
  await page.locator('[data-agent-id="planner"]').getByTestId('agent-edit').click()
  const box = page.getByTestId('agent-edit-resources')
  await expect(box.getByTestId('agent-resource-r1')).toBeChecked()
  await expect(box.getByTestId('agent-resource-r2')).not.toBeChecked()
  await expect(box.getByTestId('agent-resource-r3')).toHaveCount(0)
  await box.getByTestId('agent-resource-r2').check()
  await box.getByTestId('agent-resources-save').click()
  await expect(box.getByTestId('agent-resources-msg')).toContainText('saved')
  expect(putBody).toEqual({ resource_ids: ['r1', 'r2'] })
})
