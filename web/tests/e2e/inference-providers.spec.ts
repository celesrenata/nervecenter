import { expect, test, type Page } from '@playwright/test'

/**
 * AC-WEBUI (providers feature) — durable inference providers + combos.
 *
 * Exercises the *persistent* registry surface (backend/api/endpoints/
 * inference_durable.py) driven by web/src/views/ProvidersView.tsx. Three new
 * behaviors are under test:
 *
 *   1. Ad-hoc pre-save probe — the provider form's "Fetch models" button
 *      (POST /api/inference/durable/providers/probe) recalls a model list with
 *      unpersisted credentials and merges it into the comma-separated models
 *      field WITHOUT saving a provider row.
 *   2. Provider create round-trip (POST /providers → re-list).
 *   3. Combo create with the searchable ModelPicker (POST /combos).
 *
 * The durable endpoints are mocked so the tests are backend-independent and
 * deterministic — the e2e mock backend (global-setup.mjs) boots the real app
 * with no durable registry. Auth still runs against the real backend
 * (break-glass login, enabled in global-setup.mjs).
 */

const API = '**/api/inference/durable'

const SEED_PROVIDER = {
  id: 'prov-1',
  name: 'OpenAI Prod',
  provider_type: 'openai',
  base_url: 'https://api.openai.com/v1',
  api_key: '••••••••',
  key_configured: true,
  capabilities: [],
  models: ['gpt-4o', 'gpt-4o-mini'],
  model_count: 2,
  created_at: '2026-11-03T00:00:00Z',
  updated_at: '2026-11-03T00:00:00Z',
}

const NOW = '2026-11-03T00:00:00Z'

/**
 * Seed a real access token the same way views.spec.ts / inference-control do:
 * mint one through the break-glass login and write it to the sessionStorage
 * key the auth transport actually reads (src/api/client.ts: `nc.accessToken`).
 */
async function seedAuth(page: Page) {
  const login = await fetch('http://127.0.0.1:8000/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: 'local-admin',
      password: 'e2e-breakglass',
    }),
  })
  if (!login.ok) {
    throw new Error(`seedAuth: break-glass login failed (HTTP ${login.status})`)
  }
  const { access_token } = (await login.json()) as { access_token: string }
  await page.addInitScript((token: string) => {
    sessionStorage.setItem('nc.accessToken', token)
  }, access_token)
}

/**
 * Collect uncaught page errors (real render/runtime bugs). Console 4xx/5xx
 * from unrelated sections is intentionally NOT failed here.
 */
function trackPageErrors(page: Page) {
  const errors: string[] = []
  const on = (err: Error) => errors.push(err.message)
  page.on('pageerror', on)
  return {
    dispose: () => page.off('pageerror', on),
    assertClean: () => {
      expect(errors, `uncaught page errors: ${errors.join(' | ')}`).toEqual([])
    },
  }
}

/**
 * Install route mocks for the durable registry. Keeps mutable state so a
 * create followed by the view's re-list refresh() returns the new row.
 */
function mockDurable(
  page: Page,
  opts: { providers?: Record<string, unknown>[]; combos?: Record<string, unknown>[] } = {},
) {
  const providers: Record<string, unknown>[] = opts.providers ? [...opts.providers] : []
  const combos: Record<string, unknown>[] = opts.combos ? [...opts.combos] : []
  let probeCalls = 0

  page.route(`${API}/providers/probe`, (route) => {
    probeCalls += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        success: true,
        models_found: 2,
        models: ['gpt-4o', 'gpt-4o-mini'],
        latency_ms: 12,
      }),
    })
  })

  page.route(`${API}/providers`, (route) => {
    const req = route.request()
    if (req.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(providers),
      })
    }
    if (req.method() === 'POST') {
      const body = JSON.parse(req.postData() || '{}') as Record<string, unknown>
      const created = {
        ...SEED_PROVIDER,
        id: `prov-${providers.length + 1}`,
        name: String(body.name ?? ''),
        provider_type: String(body.provider_type ?? 'openai'),
        base_url: (body.base_url as string | null) ?? null,
        api_key: body.api_key ? '••••••••' : null,
        key_configured: !!body.api_key,
        models: (body.models as string[]) ?? [],
        model_count: ((body.models as string[]) ?? []).length,
        created_at: NOW,
        updated_at: NOW,
      }
      providers.push(created)
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(created),
      })
    }
    return route.fulfill({ status: 405, body: 'method not allowed' })
  })

  page.route(`${API}/combos`, (route) => {
    const req = route.request()
    if (req.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(combos),
      })
    }
    if (req.method() === 'POST') {
      const body = JSON.parse(req.postData() || '{}') as Record<string, unknown>
      const provider = providers.find((p) => p.id === body.provider_id)
      const created = {
        id: `combo-${combos.length + 1}`,
        name: String(body.name ?? ''),
        provider_id: String(body.provider_id ?? ''),
        provider_name: provider ? (provider.name as string) : null,
        model_id: String(body.model_id ?? ''),
        note: (body.note as string | null) ?? null,
        created_at: NOW,
        updated_at: NOW,
      }
      combos.push(created)
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(created),
      })
    }
    return route.fulfill({ status: 405, body: 'method not allowed' })
  })

  return { probeCalls: () => probeCalls }
}

test.beforeEach(async ({ page }) => {
  await seedAuth(page)
})

test('renders the durable registry empty state', async ({ page }) => {
  const errs = trackPageErrors(page)
  mockDurable(page)

  await page.goto('/providers')

  await expect(page.getByTestId('providers-section')).toBeVisible()
  await expect(page.getByText('No durable providers yet.')).toBeVisible()
  await expect(page.getByText('No combos yet.')).toBeVisible()
  // No rows exist yet.
  await expect(page.getByTestId('providers-list')).toHaveCount(0)
  await expect(page.getByTestId('combos-list')).toHaveCount(0)

  errs.assertClean()
  errs.dispose()
})

test('ad-hoc probe recalls models into the form without saving', async ({ page }) => {
  const errs = trackPageErrors(page)
  const mock = mockDurable(page)

  await page.goto('/providers')

  // Open the provider form.
  await page.getByRole('button', { name: '+ Add Provider' }).click()
  await expect(page.getByTestId('provider-form')).toBeVisible()

  // Fill unpersisted credentials (nothing is saved yet).
  await page.getByTestId('provider-form-name').fill('OpenAI Prod')
  await page.getByTestId('provider-form-type').selectOption('openai')
  await page.getByTestId('provider-form-base-url').fill('https://api.openai.com/v1')
  await page.getByTestId('provider-form-key').fill('sk-test')

  // Fire the ad-hoc probe.
  await page.getByTestId('provider-form-probe').click()

  // Success banner + recalled models.
  await expect(page.getByTestId('provider-form-probe-result')).toBeVisible()
  await expect(page.getByText('2 model(s) reachable in 12 ms')).toBeVisible()

  // The recalled models merge into the comma-separated models field.
  await expect(page.getByTestId('provider-form-models')).toHaveValue('gpt-4o, gpt-4o-mini')

  // The probe must NOT have persisted a provider row.
  expect(mock.probeCalls()).toBe(1)
  await expect(page.getByText('No durable providers yet.')).toBeVisible()

  errs.assertClean()
  errs.dispose()
})

test('creates a durable provider and re-lists it', async ({ page }) => {
  const errs = trackPageErrors(page)
  mockDurable(page)

  await page.goto('/providers')

  await page.getByRole('button', { name: '+ Add Provider' }).click()
  await page.getByTestId('provider-form-name').fill('Anthropic Prod')
  await page.getByTestId('provider-form-type').selectOption('anthropic')
  await page.getByTestId('provider-form-base-url').fill('https://api.anthropic.com/v1')
  await page.getByTestId('provider-form-key').fill('sk-ant-test')
  await page.getByRole('button', { name: 'Create provider' }).click()

  // The new row appears after the form closes and the list refreshes.
  await expect(page.getByTestId('provider-form')).toHaveCount(0)
  await expect(page.getByTestId('providers-list')).toBeVisible()
  await expect(page.getByText('Anthropic Prod')).toBeVisible()
  await expect(page.getByText('anthropic', { exact: true })).toBeVisible()
  // A key was stored, so the masked badge (not the "no key" badge) shows.
  await expect(page.getByText('key ••••••••')).toBeVisible()

  errs.assertClean()
  errs.dispose()
})

test('creates a combo via the searchable ModelPicker', async ({ page }) => {
  const errs = trackPageErrors(page)
  mockDurable(page, { providers: [SEED_PROVIDER] })

  await page.goto('/providers')

  // The seeded provider is listed, so the combo form can reference it.
  await expect(page.getByTestId('providers-list')).toBeVisible()
  await expect(page.getByText('OpenAI Prod')).toBeVisible()

  // Open the combo form.
  await page.getByRole('button', { name: '+ Add Combo' }).click()
  await expect(page.getByTestId('combo-form')).toBeVisible()

  await page.getByTestId('combo-form-name').fill('gpt4o')
  await page.getByTestId('combo-form-provider').selectOption('prov-1')

  // Open the ModelPicker combobox and pick a model from the filtered list.
  const model = page.getByTestId('combo-form-model')
  await model.click()
  await page.getByTestId('combo-form-model-option-0').click()
  await expect(model).toHaveValue('gpt-4o')

  await page.getByRole('button', { name: 'Create combo' }).click()

  // The new combo row shows the resolved provider name and model.
  await expect(page.getByTestId('combo-form')).toHaveCount(0)
  await expect(page.getByTestId('combos-list')).toBeVisible()
  await expect(page.getByText('gpt4o')).toBeVisible()
  await expect(page.getByText('OpenAI Prod · gpt-4o')).toBeVisible()

  errs.assertClean()
  errs.dispose()
})
