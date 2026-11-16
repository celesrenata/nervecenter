import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Research view with a mocked /api/research/* (NS-ADR-103). */
const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })

const base = {
  id: 'r1', title: 'Heat pumps', question: 'Which heat pump?', budget_hours: 4, cadence_seconds: 300,
  combo: 'secure/planner', spent_seconds: 12, error: null, notes: null, has_report: false,
  next_step_at: '2026-11-16T10:00:00+00:00', created_at: '2026-11-16T09:00:00+00:00', finished_at: null,
  steps_total: 2, steps_done: 1,
}
const steps = [
  { id: 's1', ordinal: 0, goal: 'Survey models', status: 'done', result: 'Three models stand out.', attempts: 1, finished_at: null },
  { id: 's2', ordinal: 1, goal: 'Compare cost', status: 'pending', result: null, attempts: 0, finished_at: null },
]

test.describe('Research', () => {
  test('lists a project, shows progress, pauses and resumes', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(String(e)))
    let status = 'running'
    let paused = 0
    let resumed = 0
    await page.route('**/api/research', (r) => r.fulfill(json({ projects: [{ ...base, status }] })))
    await page.route('**/api/research/r1', (r) =>
      r.fulfill(json({ ...base, status, steps, working_notes: 'Step 1 (Survey models): Three models', checkpoint_cursor: 1, checkpointed_at: null })))
    await page.route('**/api/research/r1/pause', (r) => { paused++; status = 'paused'; return r.fulfill(json({ ...base, status })) })
    await page.route('**/api/research/r1/resume', (r) => { resumed++; status = 'running'; return r.fulfill(json({ ...base, status })) })
    await seedBreakGlassSession(page)
    await page.goto('/research', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Research' })).toBeVisible()
    await page.getByRole('button', { name: 'Heat pumps' }).click()
    await expect(page.getByTestId('res-progress')).toContainText('Running: 1 of 2 steps. Model secure/planner.')
    await expect(page.getByTestId('res-steps')).toContainText('Compare cost')
    await expect(page.getByTestId('res-working-notes')).toContainText('Three models')
    await page.getByRole('button', { name: 'Pause' }).click()
    await expect(page.getByTestId('res-progress')).toContainText('Paused')
    expect(paused).toBe(1)
    await page.getByRole('button', { name: 'Resume' }).click()
    await expect(page.getByTestId('res-progress')).toContainText('Running')
    expect(resumed).toBe(1)
    expect(errors).toEqual([])
  })

  test('shows the final report for a finished project and creates a project', async ({ page }) => {
    let created: Record<string, unknown> | null = null
    await page.route('**/api/research', async (r) => {
      if (r.request().method() === 'POST') {
        created = r.request().postDataJSON()
        return r.fulfill(json({ ...base, id: 'r2' }, 201))
      }
      return r.fulfill(json({ projects: [{ ...base, status: 'done', has_report: true, steps_done: 2 }] }))
    })
    await page.route('**/api/research/r1', (r) =>
      r.fulfill(json({ ...base, status: 'done', steps_done: 2, steps, working_notes: '', checkpoint_cursor: 2, checkpointed_at: null })))
    await page.route('**/api/research/r1/report', (r) => r.fulfill(json({ id: 'r1', status: 'done', report: 'Pick model B.' })))
    await seedBreakGlassSession(page)
    await page.goto('/research', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Heat pumps' }).click()
    await expect(page.getByTestId('res-report')).toContainText('Pick model B.')
    const form = page.getByTestId('res-new')
    await form.getByLabel('Title').fill('Solar')
    await form.getByLabel('Question').fill('Is solar worth it?')
    await form.getByRole('button', { name: 'Start project' }).click()
    await expect.poll(() => created).toMatchObject({ title: 'Solar', question: 'Is solar worth it?', budget_hours: 4, cadence_seconds: 300 })
  })
})
