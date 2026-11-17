import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/** Kube page (NS-ADR-120): issues, known down, protected override, settings and Check now. */
const KEY = 'pod/longhorn-system/mgr-1/CrashLoopBackOff'

test('lists issues, overrides a protected one, saves settings and checks now', async ({ page }) => {
  const sent: string[] = []
  const settings = {
    protected_namespaces: ['longhorn-system'], known_down: ['stonks-beta'], override_hours: 24,
    retention_daily: 7, retention_weekly: 4, daily_run_cap: 8, issue_cooldown_hours: 6,
    prometheus_url: null, diagnose_only_until_first_report: true,
    last_check_at: '2030-01-01T04:00:00Z', last_waste_at: null, updated_by: null,
  }
  let overrides: Record<string, unknown>[] = []
  const issue = (key: string, ns: string, name: string, mode: string, override: unknown = null) => ({
    key, namespace: ns, kind: 'pod', name, reason: 'CrashLoopBackOff', severity: 'critical', mode,
    first_seen: '2030-01-01T03:00:00Z', last_seen: '2030-01-01T04:00:00Z', evidence: { restarts: 7 }, override,
  })
  const issues = () => [
    issue(KEY, 'longhorn-system', 'mgr-1', overrides.length ? 'normal' : 'diagnose_only', overrides[0] ?? null),
    issue('pod/stonks-beta/x/Error', 'stonks-beta', 'x', 'known_down'),
  ]
  await page.route('**/api/kube/status', (r) => r.fulfill({ json: {
    settings, last_check_at: settings.last_check_at, last_waste_at: null,
    monitor: { running: false, last: null }, counts: { open: 1, known_down: 1, diagnose_only: 1, overrides: 0 } } }))
  await page.route('**/api/kube/issues', (r) => r.fulfill({ json: { issues: issues(), overrides } }))
  await page.route('**/api/kube/waste', (r) => r.fulfill({ json: { report: { created_at: '2030-01-01T04:00:00Z', data: {
    collected_at: '2030-01-01T04:00:00Z', metrics_available: true,
    idle_pods: { count: 1, items: [{ namespace: 'apps', pod: 'idle-1' }] },
    oversized_pods: { count: 0, items: [] }, unused_pvcs: { count: 0, items: [] },
    orphaned_pvs: { count: 0, items: [] }, restarting_pods: { count: 0, items: [] },
    crash_loop_burn: { estimate: true, formula: 'x', count: 0, items: [] }, not_measured: [], warnings: [] } } } }))
  await page.route(`**/api/kube/issues/${encodeURIComponent(KEY)}/override`, (r) => {
    sent.push(`override ${r.request().method()}`)
    const row = { id: 'o1', issue_key: KEY, namespace: 'longhorn-system', created_by: 'celes',
      created_at: '2030-01-01T04:05:00Z', expires_at: '2030-01-02T04:05:00Z', revoked_at: null }
    overrides = [row]
    return r.fulfill({ status: 201, json: row })
  })
  await page.route('**/api/kube/settings', (r) => {
    sent.push(r.request().postData() ?? '')
    return r.fulfill({ json: settings })
  })
  await page.route('**/api/kube/check', (r) => {
    sent.push('check')
    return r.fulfill({ json: { new: 0 } })
  })
  page.on('dialog', (d) => {
    sent.push(`confirm: ${d.message().split('\n')[0]}`)
    void d.accept()
  })
  await seedBreakGlassSession(page)
  await page.goto('/kube', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Kube' })).toBeVisible()
  await expect(page.getByTestId(`kube-issue-${KEY}`)).toContainText('protected: diagnosing only')
  await expect(page.getByTestId('kube-known-down')).toContainText('stonks-beta/x')
  await expect(page.getByTestId('kube-known-down-list')).toContainText('stonks-beta')
  await expect(page.getByTestId('kube-protected-list')).toContainText('longhorn-system')
  await expect(page.getByTestId('kube-waste')).toContainText('idle-1')
  await expect(page.getByTestId('kube-first-run')).toBeVisible()

  await page.getByTestId(`kube-override-${KEY}`).click()
  await expect(page.getByTestId(`kube-issue-${KEY}`)).toContainText('Override by celes')
  await expect(page.getByTestId('kube-override-row-o1')).toContainText('expires')
  await expect(page.getByTestId(`kube-override-${KEY}`)).toHaveCount(0)

  await page.getByLabel('Add to Known down namespaces').fill('comfyui-service')
  await page.getByRole('button', { name: 'Add' }).first().click()
  await page.getByTestId('kube-save-settings').click()
  await page.getByTestId('kube-check-now').click()
  await expect.poll(() => sent.includes('check')).toBe(true)

  expect(sent[0]).toBe('confirm: Lets the agent propose a fix plan for this issue for 24 hours. Every step still needs your approval.')
  expect(sent[1]).toBe('override POST')
  const saved = JSON.parse(sent[2])
  expect(saved.known_down).toEqual(['stonks-beta', 'comfyui-service'])
  expect(saved.prometheus_url).toBeNull()
})
