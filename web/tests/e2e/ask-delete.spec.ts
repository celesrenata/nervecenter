import { expect, test, type Page } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * Ask calendar deletion card. The stream and the confirm/cancel routes are
 * mocked: no real calendar event can be touched.
 */
const ROWS = [
  {
    id: 'r1', kind: 'series', title: 'Therapy with Laura', start: '2026-04-07T17:00:00Z', end: '2026-04-07T18:00:00Z',
    account: { id: 'a1', label: 'Frameshift', can_write: true, connection_id: 'c1' }, confidence: 'strong', selected: true,
    repeats: 'repeats weekly', next_dates: ['2026-06-09T17:00:00Z', '2026-06-16T17:00:00Z'], upcoming_count: 52,
    upcoming_capped: true, path: 'series_end', path_label: 'Ends the series as of today. Past occurrences stay.', fallback: null,
  },
  {
    id: 'r2', kind: 'single', title: 'Laura follow-up', start: '2026-06-20T18:00:00Z', end: '2026-06-20T19:00:00Z',
    account: { id: 'a1', label: 'Frameshift', can_write: true, connection_id: 'c1' }, confidence: 'strong', selected: true,
    repeats: null, next_dates: ['2026-06-20T18:00:00Z'], upcoming_count: 1, upcoming_capped: false, path: 'delete',
    path_label: 'Deletes this event.', fallback: null,
  },
  {
    id: 'r3', kind: 'single', title: 'Hour', start: '2026-06-15T16:00:00Z', end: '2026-06-15T17:00:00Z',
    account: { id: 'a1', label: 'Frameshift', can_write: true, connection_id: 'c1' }, confidence: 'possible', selected: false,
    repeats: null, next_dates: ['2026-06-15T16:00:00Z'], upcoming_count: 1, upcoming_capped: false, path: 'delete',
    path_label: 'Deletes this event.', fallback: null,
  },
]

function proposal(scope = 'all_future') {
  return {
    id: 'del-1', person: 'Laura', scope, rows: ROWS, requires_confirmation: true,
    scope_choices: scope === 'ambiguous' ? [{ id: 'next', label: 'Only the next one' }, { id: 'all_future', label: 'All future ones' }] : [],
  }
}

async function setup(page: Page, scope = 'all_future') {
  const posts: Array<{ url: string; body: unknown }> = []
  await page.route('**/api/context/ask/stream', (route) => {
    const answer = 'I found 3 entries. Nothing is deleted until you press Confirm.'
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body:
        `event: meta\ndata: {"request_id":"r"}\n\n` +
        `event: delta\ndata: ${JSON.stringify({ text: answer })}\n\n` +
        `event: delete_proposal\ndata: ${JSON.stringify(proposal(scope))}\n\n` +
        `event: done\ndata: ${JSON.stringify({ answer, status: 'delete_proposal', delete_proposal: proposal(scope) })}\n\n`,
    })
  })
  await page.route('**/api/calendar/deletions/**', async (route) => {
    const req = route.request()
    posts.push({ url: req.url(), body: req.postDataJSON() })
    if (req.url().endsWith('/cancel')) return route.fulfill({ json: { status: 'cancelled' } })
    const ids = (req.postDataJSON() as { item_ids: string[] }).item_ids
    return route.fulfill({
      json: {
        status: 'done', proposal_id: 'del-1', summary: `${ids.length} of ${ids.length} item(s) removed.`,
        results: ids.map((id) => ({
          id, title: ROWS.find((r) => r.id === id)!.title,
          status: id === 'r1' ? 'series_ended' : 'deleted', detail: 'ok',
        })),
      },
    })
  })
  await seedBreakGlassSession(page)
  await page.goto('/ask', { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('Type your message...').fill('I no longer see Laura for therapy, can you remove all my future appointments with her?')
  await page.getByRole('button', { name: 'Send' }).click()
  await expect(page.getByTestId('calendar-deletion')).toBeVisible()
  return posts
}

test('the card lists every row and nothing is posted until Confirm', async ({ page }) => {
  const posts = await setup(page)
  await expect(page.getByTestId('cal-delete-row')).toHaveCount(3)
  await expect(page.getByText('Series, repeats weekly')).toBeVisible()
  await expect(page.getByText('Ends the series as of today. Past occurrences stay.')).toBeVisible()
  await expect(page.getByTestId('cal-delete-unsure')).toContainText('Hour')
  const checks = page.getByTestId('cal-delete-check')
  await expect(checks.nth(0)).toBeChecked()
  await expect(checks.nth(1)).toBeChecked()
  await expect(checks.nth(2)).not.toBeChecked()
  await page.waitForTimeout(200)
  expect(posts).toEqual([])
})

test('Confirm posts only the checked ids and shows the result', async ({ page }) => {
  const posts = await setup(page)
  await page.getByTestId('cal-delete-check').nth(1).uncheck()
  await page.getByTestId('cal-delete-confirm').click()
  await expect(page.getByTestId('cal-delete-result')).toContainText('1 of 1 item(s) removed.')
  await expect(page.getByTestId('cal-delete-result')).toContainText('Series ended as of today')
  expect(posts).toHaveLength(1)
  expect(posts[0].url).toContain('/api/calendar/deletions/del-1/confirm')
  expect((posts[0].body as { item_ids: string[] }).item_ids).toEqual(['r1'])
})

test('Cancel posts cancel and deletes nothing', async ({ page }) => {
  const posts = await setup(page)
  await page.getByTestId('cal-delete-cancel').click()
  await expect(page.getByTestId('cal-delete-result')).toContainText('Nothing was deleted')
  expect(posts).toHaveLength(1)
  expect(posts[0].url).toContain('/cancel')
})

test('ambiguous wording disables Confirm until a choice is made', async ({ page }) => {
  const posts = await setup(page, 'ambiguous')
  await expect(page.getByTestId('cal-delete-confirm')).toBeDisabled()
  await page.getByTestId('cal-delete-scope-next').check()
  await expect(page.getByTestId('cal-delete-confirm')).toBeEnabled()
  await page.getByTestId('cal-delete-confirm').click()
  await expect(page.getByTestId('cal-delete-result')).toBeVisible()
  expect((posts[0].body as { scope: string }).scope).toBe('next')
})
