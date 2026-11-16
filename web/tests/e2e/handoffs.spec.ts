import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Handoffs view with a mocked /api/handoffs/* (NS-ADR-106). */
const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })
const base = {
  id: 'h1', target: 'opencode', runner: 'central', title: 'Fix lint', brief: 'Run ruff', repo: 'nervcenter',
  project_slug: null, requested_by: 'agent:triage', status: 'queued', push_approved: false, claimed_by: null,
  finished_at: null, result_summary: null, error: null, created_at: null,
}

test.describe('Handoffs', () => {
  test('shows the queue and a result, approves push, queues a Menagerie handoff', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(String(e)))
    let item = { ...base }
    let created: Record<string, unknown> | null = null
    await page.route('**/api/handoffs/status', (r) => r.fulfill(json({
      central: { configured: false, running: false, busy: false, url: null },
      mac: { pull_path_enabled: false, note: '' },
    })))
    await page.route('**/api/handoffs', async (r) => {
      if (r.request().method() === 'POST') {
        created = r.request().postDataJSON()
        return r.fulfill(json({ ...base, id: 'h2' }, 201))
      }
      return r.fulfill(json({ handoffs: [item, { ...base, id: 'h0', title: 'Done one', status: 'done', result_summary: 'All green.' }] }))
    })
    await page.route('**/api/handoffs/h1/approve-push', (r) => { item = { ...item, push_approved: true }; return r.fulfill(json(item)) })
    await seedBreakGlassSession(page)
    await page.goto('/handoffs', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Coding handoffs' })).toBeVisible()
    await expect(page.getByTestId('ho-runners')).toContainText('Central opencode: not configured')
    await expect(page.getByTestId('ho-h0').getByTestId('ho-result')).toContainText('All green.')
    await page.getByTestId('ho-h1').getByRole('button', { name: 'Approve push' }).click()
    await expect(page.getByTestId('ho-h1')).toContainText('push approved')
    const form = page.getByTestId('ho-new')
    await form.getByLabel('Tool').selectOption('menagerie')
    await form.getByLabel('Title').fill('Polish UI')
    await form.getByLabel('Brief').fill('Tidy the panel')
    await form.getByRole('button', { name: 'Queue handoff' }).click()
    await expect.poll(() => created).toMatchObject({ target: 'menagerie', title: 'Polish UI', brief: 'Tidy the panel' })
    expect(errors).toEqual([])
  })
})
