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
