import { expect, test } from '@playwright/test'

/**
 * S102 auth-transport round (gap closure): ApiError surfacing, Bearer
 * injection, and the /auth/callback PKCE exchange.
 *
 * Runs against the Vite dev server (VITE_API_BASE=/api, see
 * playwright.config.ts). The API layer is exercised by dynamically importing
 * the app's real module (`/src/api/client.ts`) from the page context, so the
 * tests assert the *same* module instance the views use. Backend responses
 * are overridden with `page.route` so the suite is deterministic and does not
 * depend on the mock backend's data.
 *
 * Coverage:
 *   1. non-2xx rejection shape — ApiError with HTTP status + FastAPI detail
 *      (gates OV-1 / TA-1 / DC-1: no silent `[]` on errors);
 *   2. Bearer injection — `Authorization: Bearer <nc.accessToken>` from
 *      sessionStorage, and its absence without a token;
 *   3. 401 handling — session cleared, ApiError(401) surfaced, no refresh
 *      attempt (the backend exposes no refresh route — backend/api/auth.py);
 *   4. /auth/callback — state validation, PKCE code exchange, token storage,
 *      redirect to the post-login route '/';
 *   5. /auth/callback CSRF — a state mismatch is rejected, nothing stored.
 *
 * Note: the backend data endpoints do not require auth, so the views remain
 * reachable; the App is intentionally ungated (gating decision pending — see
 * web/src/components/RequireAuth.tsx).
 */

const FAKE_ACCESS = 'tok-e2e-access'

/** Load the app shell (real mode) and confirm the real (not mock) client is active. */
async function loadClient(page: import('@playwright/test').Page) {
  await page.goto('/login', { waitUntil: 'domcontentloaded' })
  const info = await page.evaluate(async () => {
    const mod = await import('/src/api/client.ts')
    return { useMockMode: mod.useMockMode }
  })
  // VITE_API_BASE=/api in the dev server → the real API client must be live,
  // otherwise these tests would exercise the in-browser mock fixtures.
  expect(info.useMockMode).toBe(false)
}

test.describe('auth transport (S102)', () => {
  test('realApi rejects non-2xx with ApiError carrying status + FastAPI detail', async ({
    page,
  }) => {
    await loadClient(page)

    // /api/tasks → 500 with the FastAPI error envelope.
    await page.route('**/api/tasks*', (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Tasks service not configured (NERVE_DB_URL unset).' }),
      }),
    )

    const result = await page.evaluate(async () => {
      const client = await import('/src/api/client.ts')
      const err = await client.tasksApi.list().catch((e) => e)
      return {
        isApiError: err instanceof client.ApiError,
        isSilentEmpty: err === undefined,
        name: err?.name ?? null,
        status: (err as { status?: number })?.status ?? null,
        detail: (err as { detail?: string })?.detail ?? null,
      }
    })

    // The old defect: errors degraded to `[]` and rendered as silent zeros.
    expect(result.isSilentEmpty).toBe(false)
    expect(result.isApiError).toBe(true)
    expect(result.name).toBe('ApiError')
    expect(result.status).toBe(500)
    expect(result.detail).toContain('NERVE_DB_URL')
  })

  test('apiFetch injects Authorization: Bearer <nc.accessToken> only when a token is present', async ({
    page,
  }) => {
    await loadClient(page)

    let capturedAuth: string | null = null
    await page.route('**/api/health*', (route) => {
      capturedAuth = route.request().headers()['authorization'] ?? null
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ok' }),
      })
    })

    // No token → no Authorization header.
    const none = await page.evaluate(async () => {
      sessionStorage.removeItem('nc.accessToken')
      const client = await import('/src/api/client.ts')
      const res = await client.apiFetch('/health')
      return res.status
    })
    expect(none).toBe(200)
    expect(capturedAuth).toBeNull()

    // Token in sessionStorage → Bearer header injected.
    const withToken = await page.evaluate(async () => {
      sessionStorage.setItem('nc.accessToken', 'tok-e2e-access')
      const client = await import('/src/api/client.ts')
      const res = await client.apiFetch('/health')
      return res.status
    })
    expect(withToken).toBe(200)
    expect(capturedAuth).toBe(`Bearer ${FAKE_ACCESS}`)
  })

  test('401 clears the sessionStorage session, surfaces ApiError(401), and does not attempt a refresh', async ({
    page,
  }) => {
    await loadClient(page)

    const requestUrls: string[] = []
    page.on('request', (req) => requestUrls.push(req.url()))

    await page.route('**/api/goals*', (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'missing bearer token' }),
      }),
    )

    // The string is inlined (not FAKE_ACCESS) because page.evaluate serializes
    // the function into the browser context where spec-file consts don't exist.
    const result = await page.evaluate(async () => {
      sessionStorage.setItem('nc.accessToken', 'tok-e2e-access')
      sessionStorage.setItem('nc.refreshToken', 'tok-e2e-refresh')
      const client = await import('/src/api/client.ts')
      const err = await client.goalsApi.list().catch((e) => e)
      return {
        isApiError: err instanceof client.ApiError,
        status: (err as { status?: number })?.status ?? null,
        detail: (err as { detail?: string })?.detail ?? null,
        accessLeft: sessionStorage.getItem('nc.accessToken'),
        refreshLeft: sessionStorage.getItem('nc.refreshToken'),
      }
    })

    expect(result.isApiError).toBe(true)
    expect(result.status).toBe(401)
    expect(result.detail).toBe('missing bearer token')
    // Unrecoverable 401 (no backend refresh route) → session cleared.
    expect(result.accessLeft).toBeNull()
    expect(result.refreshLeft).toBeNull()
    // No refresh attempt was made anywhere.
    expect(requestUrls.some((u) => u.includes('refresh'))).toBe(false)
  })

  test('/auth/callback validates state, exchanges the PKCE code, stores tokens, and redirects to /', async ({
    page,
  }) => {
    // Seed the in-flight PKCE challenge as the authorization step would.
    await page.addInitScript(() => {
      sessionStorage.setItem('nc.pkce.codeVerifier', 'verifier-e2e')
      sessionStorage.setItem('nc.pkce.state', 'state-e2e-ok')
    })

    // The code→token exchange is now *server-side* (the OIDC client is
    // confidential), so the browser POSTs to the backend route, not the
    // provider's token endpoint. Intercept the backend route (no live backend
    // or provider in e2e); only that route is stubbed, never app loads.
    const tokenPosts: Array<Record<string, string>> = []
    await page.route('**/api/auth/sso/token', (route) => {
      tokenPosts.push(JSON.parse(route.request().postData() ?? '{}'))
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          access_token: 'sso-access-1',
          refresh_token: 'sso-refresh-1',
          token_type: 'Bearer',
          expires_in: 300,
        }),
      })
    })
    // Deterministic SSO config (does not depend on the live provider state).
    await page.route('**/api/auth/sso/config', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          enabled: true,
          issuer: 'https://sso.example.test/',
          client_id: 'nervecenter-e2e',
          authorization_endpoint: 'https://sso.example.test/authorize/',
          token_endpoint: 'https://sso.example.test/token/',
        }),
      }),
    )

    await page.goto('/auth/callback?code=code-e2e-xyz&state=state-e2e-ok', {
      waitUntil: 'domcontentloaded',
    })
    // Post-login route (default '/') after the exchange succeeds.
    await page.waitForURL('http://localhost:3000/', { timeout: 10_000 })

    const stored = await page.evaluate(() => ({
      access: sessionStorage.getItem('nc.accessToken'),
      refresh: sessionStorage.getItem('nc.refreshToken'),
      verifier: sessionStorage.getItem('nc.pkce.codeVerifier'),
    }))
    expect(stored.access).toBe('sso-access-1')
    expect(stored.refresh).toBe('sso-refresh-1')
    // The code verifier is consumed by the exchange (single use).
    expect(stored.verifier).toBeNull()

    // The token request carried the PKCE code + verifier (S256 round-trip).
    // The client id / grant type are added by the backend (confidential
    // client), so the browser request omits them.
    expect(tokenPosts).toHaveLength(1)
    const tokenPost = tokenPosts[0]
    expect(tokenPost.code).toBe('code-e2e-xyz')
    expect(tokenPost.code_verifier).toBe('verifier-e2e')
    expect(tokenPost.redirect_uri).toBe('http://localhost:3000/auth/callback')
    expect(tokenPost.client_id).toBeUndefined()
  })

  test('/auth/callback rejects a state mismatch (CSRF) without storing tokens', async ({
    page,
  }) => {
    await page.addInitScript(() => {
      sessionStorage.setItem('nc.pkce.codeVerifier', 'verifier-e2e')
      sessionStorage.setItem('nc.pkce.state', 'state-e2e-expected')
    })

    await page.goto('/auth/callback?code=code-e2e-xyz&state=state-attacker', {
      waitUntil: 'domcontentloaded',
    })

    await expect(page.getByTestId('auth-callback-error')).toBeVisible()
    // Remains on the callback route — no post-login redirect.
    expect(page.url()).toContain('/auth/callback')
    const stored = await page.evaluate(() => ({
      access: sessionStorage.getItem('nc.accessToken'),
      state: sessionStorage.getItem('nc.pkce.state'),
    }))
    expect(stored.access).toBeNull()
    // The stale challenge is cleared on failure.
    expect(stored.state).toBeNull()
  })
})
