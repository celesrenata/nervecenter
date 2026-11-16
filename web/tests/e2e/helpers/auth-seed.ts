/**
 * Shared e2e auth seeding.
 *
 * `seedBreakGlassSession(page)` mints a REAL access token through the
 * break-glass login (global-setup.mjs enables NERVE_LOCAL_ADMIN /
 * NERVE_LOCAL_ADMIN_PASSWORD on the mock backend at :8000) and seeds it via
 * addInitScript into the sessionStorage key the auth transport actually
 * reads (`src/api/client.ts` → `nc.accessToken`).
 *
 * The AC005-era `localStorage.authToken` was never read by the client, so
 * specs that seeded it answered 401 on the first authenticated request;
 * with the S102 route gate (src/auth/RequireAuth.tsx) that 401 now lands
 * on /login. Specs must seed the real session instead.
 */
export async function seedBreakGlassSession(page: import('@playwright/test').Page) {
  const login = await fetch('http://127.0.0.1:8000/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: 'local-admin',
      password: 'e2e-breakglass',
    }),
  })
  if (!login.ok) {
    throw new Error(`seedBreakGlassSession: break-glass login failed (HTTP ${login.status})`)
  }
  const { access_token } = (await login.json()) as { access_token: string }
  await page.addInitScript((token: string) => {
    sessionStorage.setItem('nc.accessToken', token)
  }, access_token)
  return access_token
}

/**
 * Sign in through the real break-glass login *from the page*, so the browser
 * context also receives the HttpOnly `nc_rt` refresh cookie (NS-ADR-123), and
 * seed the access token into sessionStorage for every later navigation.
 * Returns the access token and its expiry.
 */
export async function signInWithRefreshCookie(page: import('@playwright/test').Page) {
  await page.goto('/login', { waitUntil: 'domcontentloaded' })
  const session = await page.evaluate(async () => {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'local-admin', password: 'e2e-breakglass' }),
    })
    const body = (await res.json()) as { access_token: string; expires_at: string }
    return { status: res.status, access_token: body.access_token, expires_at: body.expires_at }
  })
  if (session.status !== 200) {
    throw new Error(`signInWithRefreshCookie: login failed (HTTP ${session.status})`)
  }
  await page.addInitScript((token: string) => {
    if (!sessionStorage.getItem('nc.accessToken')) sessionStorage.setItem('nc.accessToken', token)
  }, session.access_token)
  return session
}
