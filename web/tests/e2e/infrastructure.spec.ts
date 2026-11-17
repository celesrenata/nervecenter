import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Infrastructure page (NS-ADR-120): credentials write-only, host keys confirmed, plans approved. */
const TOKEN = 'fake-kube-token-0123456789'

test('connectors, write-only credentials, host key confirmation and plan approval', async ({ page }) => {
  const sent: string[] = []
  let connectors: Record<string, unknown>[] = [
    { id: 'k1', kind: 'kubernetes', name: 'home', secret_set: false, updated_at: null, server: 'https://10.1.1.12:6443', namespace: null },
    { id: 's1', kind: 'ssh', name: 'esnixi', secret_set: true, updated_at: null, host: '192.168.42.254', port: 22, user: 'celes',
      host_key_status: 'pending', fingerprints: [], pending_fingerprints: ['SHA256:abc (ED25519)'] },
  ]
  let plan = {
    id: 'p1', agent_id: 'architecture_analyst', title: 'Restart api', summary: 'stuck pod', status: 'proposed',
    steps: [{ connector: 'home', args: ['rollout', 'restart', 'deploy/api'], purpose: 'restart' }],
    verify: [{ connector: 'home', args: ['get', 'pods'] }], rollback: 'none', act_mode: 'opencode',
    decided_by: null, decided_at: null, results: [], handoff_id: null, created_at: null,
  }
  await page.route('**/api/infra/connectors', (r) => r.fulfill({ json: { connectors } }))
  await page.route('**/api/infra/plans', (r) => r.fulfill({ json: { plans: [plan] } }))
  await page.route('**/api/infra/connectors/home/secret', (r) => {
    sent.push(r.request().postData() ?? '')
    connectors = connectors.map((c) => (c.name === 'home' ? { ...c, secret_set: true } : c))
    return r.fulfill({ json: connectors[0] })
  })
  await page.route('**/api/infra/connectors/esnixi/trust-host-key', (r) => {
    sent.push(r.request().postData() ?? '')
    connectors = connectors.map((c) => (c.name === 'esnixi' ? { ...c, host_key_status: 'pinned', fingerprints: ['SHA256:abc (ED25519)'], pending_fingerprints: [] } : c))
    return r.fulfill({ json: connectors[1] })
  })
  await page.route('**/api/infra/plans/p1/approve', (r) => {
    sent.push(r.request().postData() ?? '')
    plan = { ...plan, status: 'approved', decided_by: 'owner', handoff_id: 'h1' }
    return r.fulfill({ json: plan })
  })

  await seedBreakGlassSession(page)
  await page.goto('/infrastructure', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Infrastructure' })).toBeVisible()

  const tokenInput = page.getByTestId('infra-token-home')
  await expect(tokenInput).toHaveAttribute('type', 'password')
  await tokenInput.fill(TOKEN)
  await page.getByTestId('infra-connector-home').getByRole('button', { name: 'Save credential' }).click()
  await expect(page.getByTestId('infra-connector-home')).toContainText('credential set')
  await expect(tokenInput).toHaveValue('')
  expect(await page.content()).not.toContain(TOKEN)

  await expect(page.getByTestId('infra-pending-esnixi')).toContainText('SHA256:abc')
  await page.getByRole('button', { name: 'Trust these keys' }).click()
  await expect(page.getByTestId('infra-connector-esnixi')).toContainText('host key pinned')

  await expect(page.getByTestId('infra-plan-p1')).toContainText('kubectl rollout restart deploy/api')
  await page.getByTestId('infra-approve-p1').click()
  await expect(page.getByTestId('infra-plan-p1')).toContainText('approved')
  expect(sent).toEqual([
    JSON.stringify({ token: TOKEN }),
    JSON.stringify({ fingerprints: ['SHA256:abc (ED25519)'] }),
    JSON.stringify({ act_mode: 'opencode' }),
  ])
})

test('an SSH host without a pinned key explains the next step and keeps Test disabled', async ({ page }) => {
  await page.route('**/api/infra/connectors', (r) => r.fulfill({ json: { connectors: [
    { id: 's1', kind: 'ssh', name: 'esnixi', secret_set: true, updated_at: null, host: '192.168.133.3', port: 22,
      user: 'celes', host_key_status: 'unpinned', fingerprints: [], pending_fingerprints: [] },
  ] } }))
  await page.route('**/api/infra/plans', (r) => r.fulfill({ json: { plans: [] } }))
  await seedBreakGlassSession(page)
  await page.goto('/infrastructure', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('infra-needs-key-esnixi')).toContainText('Scan host key')
  await expect(page.getByTestId('infra-test-esnixi')).toBeDisabled()
})

test('a running plan updates its live log without a page reload', async ({ page }) => {
  const step = { connector: 'esnixi', command: 'docker build .', purpose: 'Build' }
  const plan: Record<string, unknown> = {
    id: 'p9', agent_id: 'owner', title: 'Build image', summary: '', status: 'running',
    steps: [{ connector: 'home', args: ['get', 'pods'], purpose: 'Check' }, step], verify: [],
    rollback: '', act_mode: 'opencode', decided_by: 'celes', decided_at: null, handoff_id: null,
    created_at: null,
    results: [{ index: 0, ok: true, exit: 0, output: 'pod-a Running\n' }],
    live: { index: 1, started_at: new Date().toISOString(), output: '#1 load build definition\n' },
  }
  let calls = 0
  await page.route('**/api/infra/connectors', (r) => r.fulfill({ json: { connectors: [] } }))
  await page.route('**/api/infra/plans', (r) => {
    calls += 1
    return r.fulfill({ json: { plans: [plan] } })
  })
  await seedBreakGlassSession(page)
  await page.goto('/infrastructure', { waitUntil: 'domcontentloaded' })
  const log = page.getByTestId('infra-plan-log-p9')
  await expect(log).toContainText('pod-a Running')
  await expect(page.getByTestId('infra-plan-live-p9')).toContainText('#1 load build definition')
  await expect(page.getByTestId('infra-plan-live-p9')).toContainText('running')

  // New output arrives through polling alone.
  plan.live = { ...(plan.live as object), output: '#1 load build definition\n#2 npm ci\n' }
  await expect(page.getByTestId('infra-plan-live-p9')).toContainText('#2 npm ci', { timeout: 6000 })

  // Step finishes and the plan completes: polling stops after the final state.
  plan.live = null
  plan.status = 'done'
  plan.results = [...(plan.results as unknown[]), { index: 1, ok: true, exit: 0, output: 'pushed\n' }]
  await expect(log).toContainText('pushed', { timeout: 6000 })
  await expect(page.getByTestId('infra-plan-live-p9')).toHaveCount(0)
  const settled = calls
  await page.waitForTimeout(4500)
  expect(calls).toBe(settled)

  await page.getByTestId('infra-plan-log-maximize-p9').click()
  await expect(page.getByTestId('maximize-overlay')).toContainText('pushed')
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('maximize-overlay')).toHaveCount(0)
})
