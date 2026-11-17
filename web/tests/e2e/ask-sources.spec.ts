import { expect, test, type Page } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * Ask shows the records an answer cites. The stream is mocked: nothing here
 * reads real data.
 */
function mockStream(page: Page, done: Record<string, unknown>, answer: string) {
  return page.route('**/api/context/ask/stream', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body:
        `event: meta\ndata: ${JSON.stringify({ request_id: 'r1' })}\n\n` +
        `event: delta\ndata: ${JSON.stringify({ text: answer })}\n\n` +
        `event: done\ndata: ${JSON.stringify({ answer, status: 'answered', ...done })}\n\n`,
    }),
  )
}

async function ask(page: Page, text: string) {
  await seedBreakGlassSession(page)
  await page.goto('/ask', { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('Type your message...').fill(text)
  await page.getByRole('button', { name: 'Send' }).click()
}

test('an answer lists the records it cites, each linking to its screen', async ({ page }) => {
  const records = [
    { id: 'cal-0123456789', domain: 'calendar', title: 'Piano lesson with Marta', when: '2026-06-03T17:00:00Z', snippet: '', link: '/calendar' },
    { id: 'mail-abcdef0123', domain: 'mail', title: 'Water invoice for May', when: '2026-06-01T12:00:00Z', snippet: '', link: '/email' },
  ]
  await mockStream(
    page,
    { records, citations: [{ record_id: 'cal-0123456789', title: 'Piano lesson with Marta' }], grounded: true },
    'Your lesson is tomorrow [cal-0123456789].',
  )
  await ask(page, 'When is my piano lesson?')
  const sources = page.getByTestId('ask-sources')
  await expect(sources).toBeVisible()
  await expect(page.getByTestId('ask-source')).toHaveCount(2)
  await expect(page.getByTestId('ask-source-link').first()).toHaveText('Piano lesson with Marta')
  await expect(page.getByTestId('ask-source-link').first()).toHaveAttribute('href', '/calendar')
  await expect(sources).not.toContainText('evidence-0000')
})

test('a not-found answer is plain text with no sources', async ({ page }) => {
  await mockStream(page, { records: [], citations: [], grounded: false }, 'I found nothing about glacier in calendar, mail.')
  await ask(page, 'What did Zebulon say about the glacier?')
  await expect(page.getByText('I found nothing about glacier')).toBeVisible()
  await expect(page.getByTestId('ask-sources')).toHaveCount(0)
})
