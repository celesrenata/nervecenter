import { expect, test, type Page } from '@playwright/test'
import { seedBreakGlassSession } from './helpers/auth-seed'

/**
 * Form fields follow the app theme, not the OS: the dark theme on a light OS
 * used to render `bg-white dark:bg-gray-700` fields white with the dark
 * theme's white text (white on white).
 */

function luminance(rgb: string): number {
  const [r, g, b] = (rgb.match(/\d+(\.\d+)?/g) ?? ['0', '0', '0']).slice(0, 3).map(Number)
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
}

async function fieldColours(page: Page, selector: string) {
  return page.locator(selector).first().evaluate((el) => {
    const cs = getComputedStyle(el)
    return { bg: cs.backgroundColor, fg: cs.color, scheme: getComputedStyle(document.documentElement).colorScheme }
  })
}

for (const [theme, os] of [
  ['dark', 'light'],
  ['light', 'dark'],
] as const) {
  test(`${theme} theme on a ${os} OS keeps field text readable`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: os })
    await page.addInitScript((t) => window.localStorage.setItem('nerve-theme', t), theme)
    await seedBreakGlassSession(page)
    await page.goto('/ask', { waitUntil: 'domcontentloaded' })
    await page.getByTestId('ask-advanced-toggle').click()
    const c = await fieldColours(page, '#ask-agent-select')
    expect(c.scheme).toBe(theme)
    const contrast = Math.abs(luminance(c.bg) - luminance(c.fg))
    expect(contrast, `bg ${c.bg} vs text ${c.fg}`).toBeGreaterThan(0.5)
    if (theme === 'dark') expect(luminance(c.bg)).toBeLessThan(0.5)
    else expect(luminance(c.bg)).toBeGreaterThan(0.5)
  })
}
