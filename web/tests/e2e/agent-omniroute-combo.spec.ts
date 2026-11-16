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
  await page.getByTestId('agent-inference').first().click()
  const picker = page.getByTestId('agent-infer-combo')
  await expect(picker.locator('option', { hasText: 'secure/coder' })).toHaveCount(1)
  await picker.selectOption('or:secure/coder')
  await page.getByTestId('agent-infer-save').click()
  await expect.poll(() => patches.length).toBe(1)
  expect(patches[0]).toEqual({ inference: { provider: 'omniroute', model: 'secure/coder' } })
})
