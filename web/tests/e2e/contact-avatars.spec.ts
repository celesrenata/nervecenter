import { expect, test } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * Contact names and photos from Mac Contacts (NS-ADR-097). The list routes
 * are stubbed; the photo route returns a 1x1 PNG for one contact and 404 for
 * the other, which must fall back to initials. Photos load through the
 * authenticated fetch (blob URLs), never a bare <img src> to the API.
 */
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

test.beforeEach(async ({ page }) => {
  await page.route('**/api/contact-photos**', (route) => {
    const url = route.request().url()
    const hasPhoto = url.includes('%2B15551234567') || url.endsWith('/api/contact-photos/p-ada')
    return hasPhoto
      ? route.fulfill({ contentType: 'image/png', body: PNG })
      : route.fulfill({ status: 404, json: { detail: 'no photo' } })
  })
  await seedBreakGlassSession(page)
})

test('messages show the contact name with photo, unknown numbers get initials', async ({ page }) => {
  const photoRequests: string[] = []
  page.on('request', (r) => r.url().includes('/api/contact-photos') && photoRequests.push(r.url()))
  const conv = (id: string, peer: string, name: string | null) => ({
    conversation_id: id, channel: 'imessage', peer_identifier: peer, peer_name: name,
    unread: 0, message_count: 1, last_body: 'hi', last_direction: 'INBOUND',
    last_timestamp: '2026-11-14T12:00:00Z', created_at: '2026-11-14T12:00:00Z',
    updated_at: '2026-11-14T12:00:00Z',
  })
  await page.route('**/api/messages**', (route) =>
    route.fulfill({ json: { data: [conv('c1', '+15551234567', 'Ada Lovelace'),
                                   conv('c2', '+15550000000', null)], meta: {} } }),
  )
  await page.goto('/messages', { waitUntil: 'domcontentloaded' })
  await expect(page.getByText('Ada Lovelace')).toBeVisible()
  await expect(page.getByTestId('contact-avatar-photo')).toHaveCount(1)
  await expect(page.getByTestId('contact-avatar-photo')).toHaveAttribute('src', /^blob:/)
  await expect(page.getByTestId('contact-avatar-initials')).toHaveCount(1)
  expect(new Set(photoRequests).size).toBe(2)
})

test('contacts list shows the stored photo by person id', async ({ page }) => {
  await page.route('**/api/contacts**', (route) =>
    route.fulfill({ json: { data: [{ person_id: 'p-ada', display_name: 'Ada Lovelace',
      emails: [], phones: ['+15551234567'], notes: null, is_vip: false,
      source: 'personal-relay', created_at: null, last_updated_at: null }], meta: {} } }),
  )
  await page.goto('/contacts', { waitUntil: 'domcontentloaded' })
  await expect(page.getByText('Ada Lovelace')).toBeVisible()
  await expect(page.getByTestId('contact-avatar-photo')).toHaveAttribute('src', /^blob:/)
})
