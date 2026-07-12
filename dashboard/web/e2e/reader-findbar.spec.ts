import { test, expect } from '@playwright/test';
import { loadManifest, openConversation, settleScroller, READER_BODY } from './utils';

// Scenario 5 (spec §4.5) — find-bar focus trap + Esc containment. Tab from the
// find input cycles within the bar controls; Esc while a bar BUTTON holds focus
// closes only the find bar — the reader stays mounted (view stays conversations,
// URL intact) and focus restores to the thread. RED lever: move Esc handling from
// the bar container back onto the input (the #217 S4 teardown bug) so Esc on a
// button bubbles to the document and tears the reader down.
const m = loadManifest();
const FINDBAR = '.conv-findbar';

test('the find bar traps Tab focus and Esc on a button closes only the bar', async ({ page }) => {
  await openConversation(page, m.long_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);
  const url0 = page.url();

  // Open the find bar; the input takes focus.
  await page.locator(READER_BODY).click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('/');
  await expect(page.locator(FINDBAR)).toBeVisible();
  await expect(page.locator('.conv-findbar-input')).toBeFocused();
  // A match enables the prev/next nav buttons (more controls in the trap).
  await page.locator('.conv-findbar-input').fill(m.jump_target_needle);
  await expect(page.locator('.conv-findbar-count')).toContainText('1 / 1');

  // Tab through more than one full cycle: focus never leaves the bar.
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press('Tab');
    const withinBar = await page.evaluate(() => {
      const a = document.activeElement;
      const bar = document.querySelector('.conv-findbar');
      return !!(a && bar && bar.contains(a));
    });
    expect(withinBar, `Tab #${i + 1} kept focus inside the find bar`).toBe(true);
  }

  // Move focus onto a bar BUTTON, then press Escape.
  await page.locator('.conv-findbar-close').focus();
  await expect(page.locator('.conv-findbar-close')).toBeFocused();
  await page.keyboard.press('Escape');

  // The find bar closed, but the reader is intact (Esc did NOT tear it down).
  await expect(page.locator(FINDBAR)).toHaveCount(0);
  await expect(page.locator(READER_BODY)).toBeVisible();
  expect(page.url(), 'still on the same conversation route').toBe(url0);
  // Focus restored to the thread (not left on a detached button / the body).
  const focusOk = await page.evaluate(() => {
    const a = document.activeElement;
    return !!a && a !== document.body && document.querySelector('.conv-reader') !== null
      && (document.querySelector('.conv-reader') as HTMLElement).contains(a);
  });
  expect(focusOk, 'focus restored inside the reader').toBe(true);
});
