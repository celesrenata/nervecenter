import { expect, test, type Page } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Workbench Phase 1 view with mocked /api/workbench/* (Req 3.4, 9.5). */
const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })
const GIB = 1024 ** 3

async function mockWorkbench(page: Page, health: { current: unknown }) {
  await page.route('**/api/workbench/config', (r) => r.fulfill(json({ phase: 1, ide_enabled: false })))
  await page.route('**/api/workbench/projects', (r) =>
    r.fulfill(json({
      projects: [
        { kind: 'opencode', slug: 'alpha', title: 'Alpha', archived: false },
        { kind: 'opencode', slug: 'old-one', title: 'Old one', archived: true },
        { kind: 'menagerie', slug: 'zoo', title: 'Zoo' },
      ],
    })))
  await page.route('**/api/workbench/unregistered', (r) => r.fulfill(json({ unregistered: [] })))
  await page.route('**/api/workbench/health', (r) => r.fulfill(json(health.current)))
}

test.describe('Workbench (Phase 1)', () => {
  test('hides archived projects until "Show archived" is ticked, and resets on leaving', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(String(e)))
    await mockWorkbench(page, { current: { projects: { mounted: true, writable: true, free_bytes: 50 * GIB } } })
    await seedBreakGlassSession(page)
    await page.goto('/workbench', { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('workbench-view')).toBeVisible()
    await expect(page.getByRole('link', { name: 'Alpha' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Zoo' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Old one' })).toHaveCount(0)
    await expect(page.getByTestId('wb-archived-badge')).toHaveCount(0)

    await page.getByTestId('wb-show-archived').check()
    await expect(page.getByRole('link', { name: 'Old one' })).toBeVisible()
    await expect(page.getByTestId('wb-archived-badge')).toHaveCount(1)

    await page.getByTestId('wb-show-archived').uncheck()
    await expect(page.getByRole('link', { name: 'Old one' })).toHaveCount(0)

    // The filter lives in component state: leaving and returning resets it.
    await page.getByTestId('wb-show-archived').check()
    await page.getByRole('link', { name: 'Alpha' }).click()
    await expect(page.getByTestId('workbench-project-view')).toBeVisible()
    await page.getByRole('link', { name: 'Back to Workbench' }).click()
    await expect(page.getByTestId('wb-show-archived')).not.toBeChecked()
    await expect(page.getByRole('link', { name: 'Old one' })).toHaveCount(0)
    expect(errors).toEqual([])
  })

  test('shows the low-space banner under 5 GiB and clears it when space recovers', async ({ page }) => {
    const health = { current: { projects: { mounted: true, writable: true, free_bytes: 1 * GIB } } as unknown }
    await page.clock.install()
    await mockWorkbench(page, health)
    await seedBreakGlassSession(page)
    await page.goto('/workbench', { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('wb-low-space')).toContainText('Low space on projects')

    health.current = { projects: { mounted: true, writable: true, free_bytes: 20 * GIB } }
    await page.clock.runFor(31_000)
    await expect(page.getByTestId('wb-low-space')).toHaveCount(0)
  })
})

/** Phase 2: Code_Editor against a stateful mock of /api/workbench/projects/{slug}/* (Req 4.7, 4.13, 8.9, 11.7). */
async function mockEditorApi(page: Page, disk: { content: string; mtime: number }) {
  await page.route('**/api/workbench/**', (r) => {
    const url = new URL(r.request().url())
    const method = r.request().method()
    const p = url.pathname
    if (p === '/api/workbench/config') return r.fulfill(json({ phase: 2, ide_enabled: false }))
    if (p === '/api/workbench/usage') {
      return r.fulfill(json({ usage: [{ id: 1, session_id: 's-1', model: 'local/any', input_tokens: 42, output_tokens: 7, recorded_at: '2026-01-01T00:00:00Z' }] }))
    }
    if (p === '/api/workbench/projects/alpha/files') {
      return r.fulfill(json({ path: '', entries: [{ name: 'notes.txt', path: 'notes.txt', type: 'file', size: disk.content.length, mtime_ns: disk.mtime }] }))
    }
    if (p === '/api/workbench/projects/alpha/file') {
      if (method === 'PUT') {
        const body = r.request().postDataJSON() as { content: string; base_mtime_ns: number }
        if (body.base_mtime_ns !== disk.mtime) return r.fulfill(json({ detail: 'stale' }, 409))
        disk.content = body.content
        disk.mtime += 1
        return r.fulfill(json({ path: 'notes.txt', mtime_ns: disk.mtime, size: disk.content.length }))
      }
      return r.fulfill(json({ path: 'notes.txt', content: disk.content, mtime_ns: disk.mtime, size: disk.content.length, type: 'file' }))
    }
    if (p === '/api/workbench/projects/alpha/sessions') return r.fulfill(json({ sessions: [] }))
    if (p === '/api/workbench/projects/alpha/changes') return r.fulfill(json({ token: 't', paths: [] }))
    return r.fulfill(json({ detail: 'not mocked' }, 404))
  })
}

const monacoText = (page: Page) => page.locator('.monaco-editor .view-lines')
const beforeUnloadPrevented = (page: Page) =>
  page.evaluate(() => {
    const ev = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(ev)
    return ev.defaultPrevented
  })

test.describe('Workbench (Phase 2 editor)', () => {
  test('edits, saves, and finds the saved content after reload; flags unsaved changes', async ({ page }) => {
    const disk = { content: 'hello', mtime: 100 }
    const hosts = new Set<string>()
    page.on('request', (req) => {
      const u = new URL(req.url())
      if (u.protocol === 'http:' || u.protocol === 'https:') hosts.add(u.host)
    })
    await mockEditorApi(page, disk)
    await seedBreakGlassSession(page)
    await page.goto('/workbench/alpha/edit', { waitUntil: 'domcontentloaded' })
    await page.getByRole('treeitem', { name: /notes\.txt/ }).getByRole('button').click()
    await expect(monacoText(page)).toContainText('hello')
    expect(await beforeUnloadPrevented(page)).toBe(false)

    await page.locator('.monaco-editor').first().click()
    await page.keyboard.press('ControlOrMeta+End')
    await page.keyboard.type(' world')
    await expect(monacoText(page)).toContainText('hello world')
    await expect(page.getByTestId('tab-dirty-notes.txt')).toBeVisible()
    await expect(page.getByTestId('tree-dirty-notes.txt')).toBeVisible()
    expect(await beforeUnloadPrevented(page)).toBe(true)

    // Monaco binds CtrlCmd+S from the user agent string, not the host OS.
    const mac = await page.evaluate(() => navigator.userAgent.includes('Macintosh'))
    await page.keyboard.press(mac ? 'Meta+s' : 'Control+s')
    await expect(page.getByTestId('tab-dirty-notes.txt')).toHaveCount(0)
    expect(disk.content).toBe('hello world')
    expect(await beforeUnloadPrevented(page)).toBe(false)

    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByRole('treeitem', { name: /notes\.txt/ }).getByRole('button').click()
    await expect(monacoText(page)).toContainText('hello world')
    await expect(page.getByTestId('tab-dirty-notes.txt')).toHaveCount(0)

    // Req 4.13: the editor loads without contacting any other host.
    const own = new URL(page.url()).host
    expect([...hosts].filter((h) => h !== own)).toEqual([])
  })

  test('shows Session usage in the model-policy panel', async ({ page }) => {
    await mockEditorApi(page, { content: '', mtime: 1 })
    await page.route('**/api/projects', (r) => r.fulfill(json({ projects: [] })))
    await page.route('**/api/model-policy', (r) => r.fulfill(json({
      local_model: 'local/any', max_local_failures: 2, grants: [],
      openai: { key_configured: false, model_id: 'gpt-4o-mini', queue_depth: 0, in_flight: 0, next_slot_at: null,
        today: { requests: 0, throttled: 0, errors: 0, input_tokens: 0, output_tokens: 0 },
        last_hour: { requests: 0, throttled: 0, errors: 0, input_tokens: 0, output_tokens: 0 },
        limits: { concurrency: 1, min_spacing_s: 20, max_requests_hour: 30, max_requests_day: 200 } },
    })))
    await seedBreakGlassSession(page)
    await page.goto('/projects', { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('session-usage')).toContainText('Session s-1: local/any, 42 in / 7 out tokens')
  })
})

/** Phase 3: Browser_IDE controls and the Handoffs "Open in Browser IDE" link (Req 5.12, 10.3, 10.4, 10.5). */
const handoff = (over: Record<string, unknown>) => ({
  id: 'h-1', target: 'menagerie', runner: 'mac', title: 'Handoff', brief: 'do it', repo: null, project_slug: null,
  requested_by: 'tester', status: 'queued', push_approved: false, claimed_by: null, finished_at: null,
  result_summary: null, error: null, created_at: '2026-01-01T00:00:00Z', ...over,
})

async function mockPhase3(page: Page, opts: { ide: boolean; handoffs?: unknown[] }) {
  const mutations: string[] = []
  await page.route(/\/api\/(workbench|handoffs)(\/|$|\?)/, (r) => {
    const req = r.request()
    const p = new URL(req.url()).pathname
    if (req.method() !== 'GET') mutations.push(`${req.method()} ${p}`)
    if (p === '/api/workbench/config') return r.fulfill(json({ phase: 3, ide_enabled: opts.ide }))
    if (p === '/api/workbench/projects') {
      return r.fulfill(json({ projects: [
        { kind: 'opencode', slug: 'alpha', title: 'Alpha' },
        { kind: 'menagerie', slug: 'zoo', title: 'Zoo' },
      ] }))
    }
    if (p === '/api/workbench/projects/opencode/alpha') return r.fulfill(json({ kind: 'opencode', slug: 'alpha', title: 'Alpha', handoffs: [] }))
    if (p === '/api/workbench/projects/menagerie/zoo') return r.fulfill(json({ kind: 'menagerie', slug: 'zoo', title: 'Zoo', handoffs: [] }))
    if (p === '/api/workbench/projects/alpha/sessions') return r.fulfill(json({ sessions: [] }))
    if (p === '/api/workbench/ide') return r.fulfill(json({ instances: [] }))
    if (p === '/api/workbench/ide/menagerie/zoo/status') return r.fulfill(json({ kind: 'menagerie', slug: 'zoo', state: 'stopped' }))
    if (p === '/api/workbench/health') return r.fulfill(json({ projects: { mounted: true, writable: true, free_bytes: 50 * GIB } }))
    if (p === '/api/handoffs') return r.fulfill(json({ handoffs: opts.handoffs ?? [] }))
    if (p === '/api/handoffs/status') {
      return r.fulfill(json({ central: { configured: false, running: false, busy: false, url: null }, mac: { pull_path_enabled: true, note: '' } }))
    }
    return r.fulfill(json({ detail: 'not mocked' }, 404))
  })
  return mutations
}

test.describe('Workbench (Phase 3 Browser_IDE)', () => {
  test('hides Browser_IDE controls when ide_enabled is false, leaving editor and sessions intact', async ({ page }) => {
    await mockPhase3(page, { ide: false })
    await seedBreakGlassSession(page)
    await page.goto('/workbench/opencode/alpha', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: /Sessions \(0\)/ })).toBeVisible()
    await expect(page.getByTestId('wb-editor-link')).toBeVisible()
    await expect(page.getByRole('region', { name: 'Browser IDE' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Start IDE' })).toHaveCount(0)

    await page.goto('/workbench/menagerie/zoo', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'Zoo' })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Browser IDE' })).toHaveCount(0)
  })

  test('shows the Browser IDE link only for a menagerie handoff with a matching menagerie project', async ({ page }) => {
    await mockPhase3(page, {
      ide: true,
      handoffs: [
        handoff({ id: 'h-match', title: 'Match', repo: 'git@host:me/zoo.git' }),
        handoff({ id: 'h-nomatch', title: 'No match', repo: 'git@host:me/other.git' }),
        handoff({ id: 'h-opencode', title: 'Opencode', target: 'opencode', runner: 'central', repo: 'git@host:me/zoo.git' }),
        handoff({ id: 'h-norepo', title: 'No repo' }),
      ],
    })
    await seedBreakGlassSession(page)
    await page.goto('/handoffs', { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('ho-h-match')).toBeVisible()
    await expect(page.getByTestId('ho-h-match').getByTestId('ho-open-ide')).toBeVisible()
    for (const id of ['h-nomatch', 'h-opencode', 'h-norepo']) {
      await expect(page.getByTestId(`ho-${id}`)).toBeVisible()
      await expect(page.getByTestId(`ho-${id}`).getByTestId('ho-open-ide')).toHaveCount(0)
    }
  })

  test('hides the Browser IDE link when the IDE is disabled', async ({ page }) => {
    await mockPhase3(page, { ide: false, handoffs: [handoff({ id: 'h-match', repo: 'zoo' })] })
    await seedBreakGlassSession(page)
    await page.goto('/handoffs', { waitUntil: 'domcontentloaded' })
    await expect(page.getByTestId('ho-h-match')).toBeVisible()
    await expect(page.getByTestId('ho-open-ide')).toHaveCount(0)
  })

  test('following the link leaves the handoff status unchanged', async ({ page }) => {
    const mutations = await mockPhase3(page, { ide: true, handoffs: [handoff({ id: 'h-match', repo: 'zoo', status: 'queued' })] })
    await seedBreakGlassSession(page)
    await page.goto('/handoffs', { waitUntil: 'domcontentloaded' })
    const status = page.getByTestId('ho-h-match').getByTestId('ho-status')
    await expect(status).toHaveText('queued')

    await page.getByTestId('ho-open-ide').click()
    await expect(page).toHaveURL(/\/workbench\/menagerie\/zoo$/)
    await expect(page.getByRole('heading', { name: 'Zoo' })).toBeVisible()

    await page.goBack()
    await expect(page.getByTestId('ho-h-match').getByTestId('ho-status')).toHaveText('queued')
    expect(mutations.filter((m) => m.includes('/api/handoffs'))).toEqual([])
  })
})
