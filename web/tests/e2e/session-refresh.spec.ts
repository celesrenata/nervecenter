import { expect, test, type Page } from '@playwright/test'
import { signInWithRefreshCookie } from './helpers/auth-seed'

/**
 * NS-ADR-123: NerveCenter-owned sessions. The web client refreshes silently at
 * ~80% of the access lifetime, refreshes once and retries on a 401, restores
 * the session after a browser restart from the HttpOnly `nc_rt` cookie, and
 * ends the session when a rotated refresh token is replayed.
 *
 * The first two tests stub the refresh route and use an SSO-shaped token (an
 * RS256 JWT with a Keycloak-like payload; the signature is never checked in
 * the browser) so the client logic is deterministic. The last three run
 * against the real e2e backend (global-setup.mjs: NERVE_COOKIE_SECURE=0,
 * refresh reuse grace 1 s).
 */
const json = (body: unknown, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

/** Keycloak-shaped RS256 access token (fake signature). */
function ssoShapedToken(expSecondsFromNow: number, jti: string): string {
  const enc = (o: unknown) => Buffer.from(JSON.stringify(o)).toString('base64url')
  const header = enc({ alg: 'RS256', typ: 'JWT', kid: 'e2e-kid' })
  const payload = enc({
    iss: 'https://sso.example.test/realms/nervecenter',
    typ: 'Bearer',
    azp: 'nervecenter',
    scope: 'openid profile email',
    jti,
    exp: Math.floor(Date.now() / 1000) + expSecondsFromNow,
  })
  return `${header}.${payload}.ZmFrZS1zaWduYXR1cmU`
}

async function loadClient(page: Page) {
  await page.goto('/login', { waitUntil: 'domcontentloaded' })
  await page.evaluate(async () => {
    await import('/src/api/client.ts')
  })
}

test('SSO-shaped token: proactive refresh fires once near expiry and the session stays', async ({
  page,
}) => {
  await loadClient(page)
  const first = ssoShapedToken(3, 'first')
  const second = ssoShapedToken(3600, 'second')
  const refreshCalls: Array<Record<string, string>> = []
  await page.route('**/api/auth/refresh', (route) => {
    refreshCalls.push(route.request().headers())
    return route.fulfill(
      json({
        access_token: second,
        token_type: 'Bearer',
        expires_in: 3600,
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
        identity: { sub: 'sso-user-1', username: 'sso-user-1', email: null },
      }),
    )
  })
  const result = await page.evaluate(async (token) => {
    const client = await import('/src/api/client.ts')
    let unauthorized = 0
    client.setUnauthorizedHandler(() => {
      unauthorized += 1
    })
    // Expiry comes from the token's own `exp` claim (3 s); refresh is due at ~2.4 s.
    client.setAccessToken(token)
    await new Promise((r) => setTimeout(r, 4_000))
    return { current: client.getAccessToken(), unauthorized }
  }, first)
  expect(refreshCalls).toHaveLength(1)
  expect(refreshCalls[0]['x-nc-refresh']).toBe('1')
  expect(result.current).toBe(second)
  expect(result.unauthorized).toBe(0)
})

test('SSO-shaped token: a 401 refreshes once and retries without logging out', async ({ page }) => {
  await loadClient(page)
  const stale = ssoShapedToken(3600, 'stale')
  const fresh = ssoShapedToken(3600, 'fresh')
  let refreshCount = 0
  const goalAuth: Array<string | undefined> = []
  await page.route('**/api/auth/refresh', (route) => {
    refreshCount += 1
    return route.fulfill(
      json({
        access_token: fresh,
        token_type: 'Bearer',
        expires_in: 3600,
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
        identity: {},
      }),
    )
  })
  await page.route('**/api/goals*', (route) => {
    const auth = route.request().headers()['authorization']
    goalAuth.push(auth)
    return auth === `Bearer ${fresh}`
      ? route.fulfill(json({ data: [] }))
      : route.fulfill(json({ detail: 'token expired' }, 401))
  })
  const result = await page.evaluate(async (token) => {
    const client = await import('/src/api/client.ts')
    let unauthorized = 0
    client.setUnauthorizedHandler(() => {
      unauthorized += 1
    })
    client.setAccessToken(token)
    // Two concurrent requests share one refresh (single-flight).
    const [a, b] = await Promise.all([client.goalsApi.list(), client.goalsApi.list()])
    return {
      a: a.status === 200,
      b: b.status === 200,
      unauthorized,
      kept: client.getAccessToken(),
    }
  }, stale)
  expect(result.a).toBe(true)
  expect(result.b).toBe(true)
  expect(result.unauthorized).toBe(0)
  expect(result.kept).toBe(fresh)
  expect(refreshCount).toBe(1)
  // Each request was tried with the stale token, then retried with the fresh one.
  expect(goalAuth.filter((a) => a === `Bearer ${stale}`)).toHaveLength(2)
  expect(goalAuth.filter((a) => a === `Bearer ${fresh}`)).toHaveLength(2)
})

test('real backend: an expired access token is refreshed from the cookie; reload and a new tab stay signed in', async ({
  page,
  context,
}) => {
  await signInWithRefreshCookie(page)
  // The refresh credential is an HttpOnly cookie scoped to /api/auth.
  const cookie = (await context.cookies()).find((c) => c.name === 'nc_rt')
  expect(cookie?.httpOnly).toBe(true)
  expect(cookie?.path).toBe('/api/auth')
  expect(await page.evaluate(() => document.cookie)).not.toContain('nc_rt')

  // Replace the access token with one the server rejects (an expired token).
  await page.evaluate(() => sessionStorage.setItem('nc.accessToken', 'expired-access-token'))
  const refreshRequests: string[] = []
  page.on('request', (req) => {
    if (req.url().includes('/api/auth/refresh')) refreshRequests.push(req.url())
  })
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Today')
  expect(page.url()).not.toContain('/login')
  expect(refreshRequests).toHaveLength(1)
  const storage = await page.evaluate(() => ({
    access: sessionStorage.getItem('nc.accessToken'),
    refresh: sessionStorage.getItem('nc.refreshToken'),
  }))
  expect(storage.access).not.toBe('expired-access-token')
  expect(storage.refresh).toBeNull()

  // Reload keeps the session.
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Today')

  // A fresh tab (empty sessionStorage, same cookie jar) restores the session
  // from the cookie: the browser-restart case.
  const second = await context.newPage()
  await second.goto('/', { waitUntil: 'domcontentloaded' })
  await expect(second.locator('main h1')).toHaveText('Today')
  expect(second.url()).not.toContain('/login')
})

test('real backend: replaying a rotated refresh cookie ends the whole session', async ({
  page,
  context,
}) => {
  await signInWithRefreshCookie(page)
  const original = (await context.cookies()).find((c) => c.name === 'nc_rt')
  expect(original).toBeTruthy()
  const refresh = () =>
    page.evaluate(async () => {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'include',
        headers: { 'X-NC-Refresh': '1' },
      })
      return res.status
    })
  // First refresh rotates the cookie.
  expect(await refresh()).toBe(200)
  const rotated = (await context.cookies()).find((c) => c.name === 'nc_rt')
  expect(rotated?.value).not.toBe(original!.value)
  // Past the reuse grace window (1 s in e2e), replay the old cookie.
  await page.waitForTimeout(1_500)
  await context.addCookies([original!])
  expect(await refresh()).toBe(401)
  // The family is revoked: even the newest cookie no longer works.
  await context.addCookies([rotated!])
  expect(await refresh()).toBe(401)
  // A new tab lands on the sign-in page.
  const fresh = await context.newPage()
  await fresh.goto('/', { waitUntil: 'domcontentloaded' })
  await fresh.waitForURL('**/login')
})

test('real backend: a missing CSRF header is refused', async ({ page }) => {
  await signInWithRefreshCookie(page)
  const status = await page.evaluate(async () => {
    const res = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' })
    return res.status
  })
  expect(status).toBe(401)
})
