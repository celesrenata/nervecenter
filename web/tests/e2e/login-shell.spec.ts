import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * Gap-closure spec — login + app shell (audit LG-1…LG-4, D-1, D-4, D-7).
 *
 * Runs under the shared harness (playwright.config.ts): mock backend on
 * :8000, Vite dev server on :3000 with VITE_API_BASE=/api. The auth
 * endpoints are intercepted per-test with page.route() so the assertions
 * do not depend on the backend's SSO/local-admin configuration.
 *
 * Auth-gated pages get `authToken` seeded in localStorage via
 * addInitScript, following the views.spec.ts convention (AuthContext
 * hydrates from that key).
 */

const AUTH_SEED = { authToken: 'test-token' }

async function seedAuth(page: Page): Promise<void> {
  await page.addInitScript((seed) => {
    for (const [k, v] of Object.entries(seed)) {
      window.localStorage.setItem(k, v)
    }
  }, AUTH_SEED)
}

function mockSsoConfig(
  page: Page,
  cfg: { enabled: boolean; client_id: string; issuer: string; authorization_endpoint?: string },
): Promise<void> {
  return page.route('**/api/auth/sso/config', async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        enabled: cfg.enabled,
        client_id: cfg.client_id,
        issuer: cfg.issuer,
        authorization_endpoint:
          cfg.authorization_endpoint ?? 'https://sso.example.com/authorize/',
        token_endpoint: 'https://sso.example.com/token/',
        jwks_uri: 'https://sso.example.com/jwks/',
      }),
    })
  })
}

const NO_DEFAULT_CREDENTIAL_TEXT = /default credentials|admin\s*\/\s*password/i

test.describe('login view (LG-1…LG-4)', () => {
  test('never advertises default credentials', async ({ page }) => {
    await mockSsoConfig(page, { enabled: false, client_id: '', issuer: '' })
    await page.goto('/login')

    const body = await page.locator('body').innerText()
    expect(body).not.toMatch(NO_DEFAULT_CREDENTIAL_TEXT)
    await expect(page.locator('text=/default credentials/i')).toHaveCount(0)
    // No SSO button while SSO is disabled (LG-4: no dead button).
    await expect(
      page.getByRole('button', { name: /sign in with sso/i }),
    ).toHaveCount(0)
  })

  test('401 shows a field-associated error on the username input', async ({
    page,
  }) => {
    await mockSsoConfig(page, { enabled: false, client_id: '', issuer: '' })
    await page.route('**/api/auth/login', (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'invalid credentials' }),
      }),
    )
    await page.goto('/login')

    await page.fill('#username', 'testuser')
    await page.fill('#password', 'wrongpass')
    await page.getByRole('button', { name: 'Sign in' }).click()

    // Error appears adjacent to the failing field and is associated via
    // aria-describedby (LG-3).
    const fieldError = page.locator('#login-credential-error')
    await expect(fieldError).toBeVisible()
    await expect(fieldError).toHaveText('Invalid credentials.')
    await expect(page.locator('#username')).toHaveAttribute(
      'aria-describedby',
      'login-credential-error',
    )
    await expect(page.locator('#password')).toHaveAttribute(
      'aria-describedby',
      'login-credential-error',
    )
  })

  test('403 surfaces the real detail in the general error area (not masked)', async ({
    page,
  }) => {
    await mockSsoConfig(page, { enabled: false, client_id: '', issuer: '' })
    await page.route('**/api/auth/login', (route) =>
      route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'local-admin login is disabled (set NERVE_LOCAL_ADMIN)',
        }),
      }),
    )
    await page.goto('/login')

    await page.fill('#username', 'admin')
    await page.fill('#password', 'whatever')
    await page.getByRole('button', { name: 'Sign in' }).click()

    // The 403 detail is shown verbatim in the general (submit) error area,
    // NOT masked by "Invalid credentials" and NOT attached to the fields.
    await expect(
      page.getByText('local-admin login is disabled (set NERVE_LOCAL_ADMIN)'),
    ).toBeVisible()
    await expect(page.locator('#login-credential-error')).toHaveCount(0)
    await expect(page.locator('#username')).not.toHaveAttribute(
      'aria-describedby',
      /login-credential-error/,
    )
  })

  test('submit button is disabled with a pending label while login() is in flight', async ({
    page,
  }) => {
    await mockSsoConfig(page, { enabled: false, client_id: '', issuer: '' })
    // Hold the request open so the pending state is observable.
    await page.route('**/api/auth/login', () => new Promise(() => {}))
    await page.goto('/login')

    await page.fill('#username', 'testuser')
    await page.fill('#password', 'whatever')
    await page.getByRole('button', { name: 'Sign in' }).click()

    const submit = page.getByRole('button', { name: /signing in/i })
    await expect(submit).toBeDisabled()
    await expect(submit).toBeVisible()
  })

  test('valid break-glass login redirects from /login into the app root', async ({
    page,
  }) => {
    await mockSsoConfig(page, { enabled: false, client_id: '', issuer: '' })
    // Simulate a valid credential exchange: the backend answers 200 with the
    // access/refresh tokens (shape per backend/api/auth.py login response).
    await page.route('**/api/auth/login', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          access_token: 'e2e-break-glass-access-token',
          refresh_token: 'e2e-break-glass-refresh-token',
          sub: 'local-admin',
          roles: ['local-admin'],
        }),
      }),
    )
    await page.goto('/login')

    await page.fill('#username', 'local-admin')
    await page.fill('#password', 'correct-horse-battery-staple')
    await page.getByRole('button', { name: 'Sign in' }).click()

    // The successful break-glass login must leave /login and land in the app
    // (the Today landing view) — not strand the user on the sign-in form.
    // This is the fix under test: AuthContext.login now navigates to '/'
    // on success, mirroring the SSO callback (AuthCallback → '/').
    await page.waitForURL((url) => url.pathname === '/')
    await expect(page.locator('main h1')).toHaveText('Today')
  })

  test('SSO button renders only when sso/config reports enabled', async ({
    page,
  }) => {
    await page.route('**/api/auth/sso/config', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          enabled: true,
          client_id: 'nervecenter',
          issuer: 'https://sso.example.com',
          authorization_endpoint: 'https://sso.example.com/authorize/',
        }),
      }),
    )
    await page.goto('/login')

    const sso = page.getByRole('button', { name: /sign in with sso/i })
    await expect(sso).toBeVisible()
  })
})

test.describe('app shell (D-1, D-4)', () => {
  test('aria-current="page" tracks the active nav item', async ({ page }) => {
    await seedAuth(page)
    await page.goto('/')

    const overview = page
      .getByRole('navigation')
      .getByRole('link', { name: 'Overview' })
    await expect(overview).toHaveAttribute('aria-current', 'page')

    // exact: 'Ask' substring-matches the 'Task DAG' nav item ("T*ask* DAG").
    await page
      .getByRole('navigation')
      .getByRole('link', { name: 'Ask', exact: true })
      .click()
    await page.waitForURL('**/ask')

    const ask = page
      .getByRole('navigation')
      .getByRole('link', { name: 'Ask', exact: true })
    await expect(ask).toHaveAttribute('aria-current', 'page')
    await expect(overview).not.toHaveAttribute('aria-current')
  })

  test('mobile (390px): content ≥70% of viewport, nav one tap away, no horizontal scroll', async ({
    page,
  }) => {
    await seedAuth(page)
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/')

    // No page-level horizontal scroll.
    await expect
      .poll(() => page.evaluate(() => document.body.scrollWidth))
      .toBeLessThanOrEqual(390)

    // Sidebar is collapsed: main content owns ≥70% of the viewport.
    const mainBox = await page.locator('main').first().boundingBox()
    expect(mainBox).not.toBeNull()
    expect(mainBox!.width).toBeGreaterThanOrEqual(0.7 * 390)

    // Navigation is one tap away: hamburger opens the drawer, a nav link
    // navigates and closes it.
    const hamburger = page.getByRole('button', { name: 'Open navigation' })
    await expect(hamburger).toBeVisible()
    await hamburger.click()

    const drawer = page.getByRole('dialog', { name: 'Navigation menu' })
    await expect(drawer).toBeVisible()
    await drawer.getByRole('link', { name: 'Systems' }).click()
    await page.waitForURL('**/systems')
    await expect(drawer).toHaveCount(0)
    await expect(hamburger).toHaveAttribute('aria-expanded', 'false')
  })
})
