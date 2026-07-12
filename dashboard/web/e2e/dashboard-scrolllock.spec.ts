import { test, expect } from '@playwright/test';

// Scenario 6 (spec §4.6) — scroll-lock under trusted wheel. With a modal open,
// a trusted `page.mouse.wheel` on the background leaves `window.scrollY ~ 0`
// (the documentElement lock); a no-modal control must scroll, proving the probe
// is non-vacuous. RED lever: lock `body` only, not `documentElement`.
//
// A short viewport guarantees the dashboard overflows so the page can scroll.
test.use({ viewport: { width: 1440, height: 500 } });

const DOCTOR = '.doctor-modal-card';

async function scrollY(page) {
  return page.evaluate(() => Math.round(window.scrollY));
}
async function wheelBackground(page) {
  // Wheel over the top-left background (away from a centered modal card).
  await page.mouse.move(20, 20);
  await page.mouse.wheel(0, 800);
}

test('a modal scroll-locks the page under a trusted wheel', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#main-content')).toBeVisible();
  await expect(page.locator('#main-content .panel-host').first()).toBeVisible();

  // Control: the page genuinely scrolls (else the lock assertion is vacuous).
  await wheelBackground(page);
  await expect.poll(() => scrollY(page)).toBeGreaterThan(0);
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect.poll(() => scrollY(page)).toBe(0);

  // Open a modal (the doctor modal engages the shared useScrollLock).
  await page.keyboard.press('d');
  await expect(page.locator(DOCTOR)).toBeVisible();

  // Locked: a trusted background wheel does NOT scroll the page.
  await wheelBackground(page);
  await wheelBackground(page);
  expect(await scrollY(page), 'page stayed locked with the modal open').toBeLessThanOrEqual(2);

  // Close the modal → the lock releases and the page scrolls again.
  await page.keyboard.press('Escape');
  await expect(page.locator(DOCTOR)).toHaveCount(0);
  await wheelBackground(page);
  await expect.poll(() => scrollY(page), { message: 'scrolls again after unlock' }).toBeGreaterThan(0);
});
