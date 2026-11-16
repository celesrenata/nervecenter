import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Projects view with a mocked /api/projects/* (NS-ADR-104). */
const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })
const proj = { slug: 'home-lab', title: 'Home lab', summary: 'Rack plans', note_count: 1, updated_at: null }

test.describe('Projects', () => {
  test('shows notes with their author, appends a note, and says when OpenAI is off', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(String(e)))
    let posted: Record<string, unknown> | null = null
    const notes = [{ id: 'n1', author: 'chatgpt', body: 'use 10GbE', created_at: null }]
    await page.route('**/api/projects', (r) => r.fulfill(json({ projects: [proj] })))
    await page.route('**/api/projects/openai/status', (r) => r.fulfill(json({ provider: 'openai', model_id: 'm', key_configured: false })))
    await page.route('**/api/projects/home-lab', (r) => r.fulfill(json({ ...proj, notes })))
    await page.route('**/api/projects/home-lab/notes', (r) => {
      posted = r.request().postDataJSON()
      notes.push({ id: 'n2', author: 'owner', body: 'order switch', created_at: null })
      return r.fulfill(json(notes[1], 201))
    })
    await seedBreakGlassSession(page)
    await page.goto('/projects', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Home lab' }).click()
    await expect(page.getByTestId('proj-notes')).toContainText('chatgpt: use 10GbE')
    await expect(page.getByTestId('proj-openai-off')).toContainText('not configured')
    await page.getByLabel('Add a note').fill('order switch')
    await page.getByRole('button', { name: 'Add note' }).click()
    await expect.poll(() => posted).toMatchObject({ body: 'order switch' })
    await expect(page.getByTestId('proj-notes')).toContainText('owner: order switch')
    expect(errors).toEqual([])
  })
})
