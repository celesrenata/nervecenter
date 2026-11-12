import { expect, test, type Page, type Route } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * security-ops.spec.ts — NerveShield Task 7.7 operations dashboard view.
 *
 * OPS-1  All sections render from the mocked /api/mail-security/ops payload;
 *        injected markup in a label renders as literal text; no remote request.
 * OPS-2  Arc NOT_CONFIGURED is stated honestly.
 * OPS-3  A safe 404 shows the empty state, no banner.
 */

const INJECT = '<img src="https://evil.example/x.png">'
const REMOTE: string[] = []

function payload(arc: unknown) {
  return {
    data: {
      health: {
        stages: [
          { stage: 'queue', status: 'HEALTHY', reasons: [] },
          { stage: 'intel_feeds', status: 'DEGRADED', reasons: ['FEED_STALE'] },
        ],
        alerts: [{ code: 'FEED_STALE', stage: 'intel_feeds', severity: 'WARNING' }],
        intel_feeds: [{ feed: 'urlhaus', feed_version: '2026-11-10', live_indicators: 812 }],
        queue: { depth: { PENDING: 0 }, oldest_pending_age_seconds: null },
      },
      review_queue: {
        pending: 4,
        by_tier: { MEDIUM: 4 },
        by_delivery: { QUEUE: 4 },
        digest: 2,
        reviewed: 1,
        quiet_hours_active: true,
      },
      counts: {
        versions: { [`2026.01|mail-security-policy@3|${INJECT}`]: 625 },
        model_runs: {
          'secure/reader|granite-4|openvino|COMPLETED': {
            count: 6,
            latency_ms_mean: 420.5,
            latency_ms_max: 900,
          },
        },
        feedback: {
          by_kind: { FALSE_POSITIVE: 1, CONFIRMED_THREAT: 3 },
          flagged_false_positive_rate: 0.25,
          missed_threat_reports: 0,
        },
        gate_holds: {},
        confirmations: { 'PENDING|PAYMENT': 1 },
      },
      runner: { running: true, failures: { SCOPE_DRAIN_FAILED: 2 } },
      arc,
    },
  }
}

async function visit(page: Page, body: unknown, status = 200) {
  page.on('request', (req) => {
    const o = new URL(req.url()).origin
    if (o !== 'http://localhost:3000' && o !== 'http://127.0.0.1:3000') REMOTE.push(req.url())
  })
  await page.route('**/api/mail-security/ops', (route: Route) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }),
  )
  await seedBreakGlassSession(page)
  await page.goto('/security-ops', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('main h1')).toHaveText('Security ops')
}

test.beforeEach(() => {
  REMOTE.length = 0
})

test('OPS-1 renders every section as inert text', async ({ page }) => {
  await visit(
    page,
    payload({
      status: 'DEGRADED',
      hosts: [
        {
          label: 'arcforge-1',
          status: 'READY',
          model: { model_id: 'qwen3-embedding-0.6b', version: 'int8', device: 'GPU' },
          activity: { queue_depth: 0, active_inferences: 1, requests_total: 5, mean_inference_seconds: 1.81 },
        },
        { label: 'arcforge-2', status: 'UNREACHABLE', code: 'CONNECT_FAILED' },
      ],
    }),
  )
  await expect(page.getByTestId('ops-health')).toContainText('DEGRADED')
  await expect(page.getByTestId('ops-health')).toContainText('FEED_STALE')
  await expect(page.getByTestId('ops-queue')).toContainText('4 pending · 2 grouped · 1 reviewed')
  await expect(page.getByTestId('ops-queue')).toContainText('Quiet hours are active.')
  await expect(page.getByTestId('ops-arc-arcforge-1')).toContainText('qwen3-embedding-0.6b (int8) on GPU')
  await expect(page.getByTestId('ops-arc-arcforge-1')).toContainText('mean 1.81 s')
  await expect(page.getByTestId('ops-arc-arcforge-2')).toContainText('UNREACHABLE')
  await expect(page.getByTestId('ops-arc')).toContainText('not a GPU-percent measure')
  await expect(page.getByTestId('ops-feedback')).toContainText('Flagged verdicts called safe: 25%')
  await expect(page.getByTestId('ops-models')).toContainText('6 · 421 / 900')
  await expect(page.getByTestId('ops-versions')).toContainText('urlhaus 2026-11-10 · 812 live')
  await expect(page.getByTestId('ops-gate')).toContainText('PENDING|PAYMENT')
  await expect(page.getByTestId('ops-runner')).toContainText('SCOPE_DRAIN_FAILED')
  // Injected markup in a label is literal text, never an element.
  await expect(page.getByTestId('ops-versions')).toContainText(INJECT)
  await expect(page.locator('main img')).toHaveCount(0)
  await page.waitForTimeout(300)
  expect(REMOTE).toEqual([])
})

test('OPS-2 Arc not configured is stated', async ({ page }) => {
  await visit(page, payload({ status: 'NOT_CONFIGURED' }))
  await expect(page.getByTestId('ops-arc')).toContainText('No ArcForge target is configured.')
})

test('OPS-3 fail closed on a safe 404', async ({ page }) => {
  await visit(page, { detail: { error_code: 'not_found' } }, 404)
  await expect(page.getByTestId('ops-empty')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})
