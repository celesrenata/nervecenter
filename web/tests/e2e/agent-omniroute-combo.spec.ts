import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Any agent (built-ins too) can run on an OmniRoute combo; it is saved as the agent's model. */
test('a built-in agent can be set to an OmniRoute combo', async ({ page }) => {
  const patches: unknown[] = []
  let agent: Record<string, unknown> = {
    id: 'planner', name: 'Planner', type: 'planner', status: 'active', builtin: true,
    description: 'Plans', tools: [], inference: null, combo_id: null,
  }
  await page.route('**/api/agents', (r) => r.fulfill({ json: { data: [agent] } }))
  await page.route('**/api/agents/omniroute-combos', (r) => r.fulfill({ json: { combos: [
    { name: 'secure/coder', enabled: true, secure: true, models: ['vllm/qwen3.8-27b-nvfp4'] },
    { name: 'local/esnixi', enabled: true, secure: false, models: ['vllm/qwen3.8-27b-nvfp4'] },
  ] } }))
  await page.route('**/api/agents/planner', async (r) => {
    if (r.request().method() === 'PATCH') {
      patches.push(r.request().postDataJSON())
      agent = { ...agent, inference: { provider: 'omniroute', model: 'secure/coder' } }
      return r.fulfill({ json: agent })
    }
    return r.fulfill({ json: agent })
  })
  await seedBreakGlassSession(page)
  await page.goto('/agents', { waitUntil: 'domcontentloaded' })
  await page.route('**/api/inference/catalog**', (r) => {
    const q = new URL(r.request().url()).searchParams.get('q') ?? ''
    const all = [{ id: 'secure/coder', owned_by: 'combo', kind: 'combo' }, { id: 'local/esnixi', owned_by: 'combo', kind: 'combo' }]
    const items = all.filter((i) => i.id.includes(q))
    return r.fulfill({ json: { total: items.length, offset: 0, limit: 50, items } })
  })
  await page.getByTestId('agent-inference').first().click()
  const search = page.getByTestId('agent-infer-combo-search')
  await search.fill('secure/c')
  await expect(page.getByTestId('agent-infer-combo-search-count')).toContainText('1 match')
  await page.getByTestId('agent-infer-combo-search-option-0').click()
  await expect(search).toHaveValue('secure/coder')
  await page.getByTestId('agent-infer-save').click()
  await expect.poll(() => patches.length).toBe(1)
  expect(patches[0]).toEqual({ inference: { provider: 'omniroute', model: 'secure/coder' } })
})

test('the model picker searches the whole ~1000-model catalog', async ({ page }) => {
  const catalog = Array.from({ length: 992 }, (_, i) => ({ id: `bedrock/model-${String(i).padStart(4, '0')}`, owned_by: 'bedrock', kind: 'model' }))
  const patches: unknown[] = []
  const agent = { id: 'planner', name: 'Planner', type: 'planner', status: 'active', builtin: true,
    description: 'Plans', tools: [], inference: null, combo_id: null }
  await page.route('**/api/agents', (r) => r.fulfill({ json: { data: [agent] } }))
  await page.route('**/api/agents/omniroute-combos', (r) => r.fulfill({ json: { combos: [] } }))
  await page.route('**/api/agents/planner', (r) => {
    if (r.request().method() === 'PATCH') patches.push(r.request().postDataJSON())
    return r.fulfill({ json: agent })
  })
  await page.route('**/api/inference/catalog**', (r) => {
    const u = new URL(r.request().url())
    const q = (u.searchParams.get('q') ?? '').toLowerCase()
    const hits = catalog.filter((m) => m.id.includes(q))
    return r.fulfill({ json: { total: hits.length, offset: 0, limit: 50, items: hits.slice(0, 50) } })
  })
  await seedBreakGlassSession(page)
  await page.goto('/agents', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('agent-inference').first().click()
  const model = page.getByTestId('agent-infer-model')
  await model.focus()
  await expect(page.getByTestId('agent-infer-model-count')).toContainText('Showing 50 of 992')
  await model.fill('model-0987')
  await expect(page.getByTestId('agent-infer-model-count')).toContainText('1 match')
  await model.press('ArrowDown')
  await model.press('Enter')
  await expect(model).toHaveValue('bedrock/model-0987')
  await page.getByTestId('agent-infer-save').click()
  await expect.poll(() => patches.length).toBe(1)
  expect(patches[0]).toEqual({ inference: { provider: null, model: 'bedrock/model-0987' } })
})
