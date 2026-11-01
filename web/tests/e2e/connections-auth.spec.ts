import { expect, test } from '@playwright/test'

/**
 * Connection Center auth-transport (gap closure): the `/api/connections`
 * client must send the shared session bearer token, and a 401 must surface
 * an honest sign-in path rather than a raw "missing bearer token" detail.
 *
 * Runs against the Vite dev server (VITE_API_BASE=/api, see
 * playwright.config.ts). The API layer is exercised by dynamically importing
 * the app's real modules (`/src/api/connections.ts`) from the page context,
 * so the tests assert the *same* module instances the views use. Backend
 * responses are overridden with `page.route` so the suite is deterministic.
 *
 * Coverage:
 *   1. Bearer injection — `Authorization: Bearer <nc.accessToken>` is attached
 *      to `connectionsApi.list()` (the shared `apiFetch` transport), exactly
 *      as for every other API module; absent without a token.
 *   2. 401 honesty — a 401 from the list route clears the sessionStorage
 *      session and rejects with `ApiError(401)`, which `isAuthRequiredError`
 *      recognizes so the view can render the sign-in path instead of the raw
 *      "missing bearer token" detail.
 */

const FAKE_ACCESS = 'tok-conn-e2e-access'

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

test.describe('connections transport auth', () => {
  test('connectionsApi.list() carries Authorization: Bearer <nc.accessToken> only when a token is present', async ({
    page,
  }) => {
    await loadClient(page)

    let capturedAuth: string | null = null
    await page.route('**/api/connections*', (route) => {
      capturedAuth = route.request().headers()['authorization'] ?? null
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ data: [], meta: null }),
      })
    })

    // No token → no Authorization header on the connections request.
    const none = await page.evaluate(async () => {
      sessionStorage.removeItem('nc.accessToken')
      const conn = await import('/src/api/connections.ts')
      const res = await conn.connectionsApi.list()
      return res.status
    })
    expect(none).toBe(200)
    expect(capturedAuth).toBeNull()

    // Token in sessionStorage → Bearer header injected on the connections request.
    const withToken = await page.evaluate(async () => {
      sessionStorage.setItem('nc.accessToken', 'tok-conn-e2e-access')
      const conn = await import('/src/api/connections.ts')
      const res = await conn.connectionsApi.list()
      return res.status
    })
    expect(withToken).toBe(200)
    expect(capturedAuth).toBe(`Bearer ${FAKE_ACCESS}`)
  })

  test('401 on the connections list clears the session and flags an auth-required error', async ({
    page,
  }) => {
    await loadClient(page)

    await page.route('**/api/connections*', (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'missing bearer token' }),
      }),
    )

    const result = await page.evaluate(async () => {
      sessionStorage.setItem('nc.accessToken', 'tok-conn-e2e-access')
      const conn = await import('/src/api/connections.ts')
      const client = await import('/src/api/client.ts')
      const err = await conn.connectionsApi.list().catch((e) => e)
      return {
        isAuthRequired: conn.isAuthRequiredError(err),
        isApiError: err instanceof client.ApiError,
        status: (err as { status?: number })?.status ?? null,
        detail: (err as { detail?: string })?.detail ?? null,
        // apiFetch's 401 handler must have cleared the session token.
        sessionCleared: sessionStorage.getItem('nc.accessToken') === null,
      }
    })

    // The 401 is terminal: session cleared, ApiError(401) surfaced, and the
    // connections client recognizes it as the sign-in path (not a raw detail).
    expect(result.sessionCleared).toBe(true)
    expect(result.isApiError).toBe(true)
    expect(result.status).toBe(401)
    expect(result.isAuthRequired).toBe(true)
  })
})
