import { test, expect } from '@playwright/test';
import {
  loadManifest, openConversation, settleScroller, scrollerMetrics,
  appendLiveTurn, uuidSel, READER_BODY, AT_BOTTOM_SLACK,
} from './utils';

// Scenario 3 (spec §4.3 + §5) — live-tail stick + pill. At the bottom, appending a
// turn to the live-tail file renders it within the bounded ingest window and the
// viewport stays stuck to bottom (followOutput). Scrolled up, another append
// surfaces the "↓ N new" pill WITHOUT moving the viewport; clicking the pill lands
// at the bottom.
//
// RED levers (both recorded in docs/superpowers/plans/2026-07-11-281-s5-red-evidence.md):
//   • pill half — make `followOutput` follow even when atBottom===false, so the
//     scrolled-up append yanks.
//   • stick half (#287 B4) — post-B1 stick is reader-owned only while
//     `followMode() === 'live'`, so stashing the machine's `settle` transition
//     (follow stays SUSPENDED after the top-open, `followOutput={false}`) breaks
//     the stick half: the at-bottom append no longer follows (measured gap 162px >
//     AT_BOTTOM_SLACK). Verified by editing `createFollowController().settle` to a
//     no-op and rebuilding; reverted — not shipped.
//
// §5 (B1 consequence): the 40-item live fixture is SINGLE-PAGE, so post-B1 it now
// opens at the TOP (the #217/#285 contract). The stick/pill halves need the
// viewport at the bottom first, so we navigate there through a real reader control
// (press `End`) and assert the at-bottom precondition before appending.
const m = loadManifest();
const PILL = '.conv-new-pill';

test('live-tail sticks at the bottom, then surfaces the pill when scrolled up', async ({ page }) => {
  await openConversation(page, m.live_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);

  // §5 precondition — a single-page live conversation now opens at the TOP (B1).
  // Drive to the bottom through a real reader control (End) so the stick/pill
  // halves start from the at-bottom state they assert.
  await page.locator(READER_BODY).click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('End');
  await settleScroller(page);
  expect((await scrollerMetrics(page)).gap, 'navigated to the bottom before the stick/pill halves').toBeLessThan(AT_BOTTOM_SLACK);

  // (1) At the bottom: an append sticks — the new turn renders and the viewport
  // stays pinned to the bottom (no pill).
  const u1 = appendLiveTurn(m);
  await expect(page.locator(uuidSel(u1)), 'the live-appended turn renders').toBeVisible({ timeout: 12_000 });
  await settleScroller(page);
  expect((await scrollerMetrics(page)).gap, 'stayed stuck to the bottom on append').toBeLessThan(AT_BOTTOM_SLACK);
  await expect(page.locator(PILL), 'no pill while stuck at bottom').toHaveCount(0);

  // (2) Scroll up (trusted wheel) so the viewport leaves the bottom.
  await page.locator(READER_BODY).hover();
  await page.mouse.wheel(0, -1500);
  await settleScroller(page);
  const scrolledTop = (await scrollerMetrics(page)).top;
  expect(scrolledTop, 'genuinely scrolled up off the bottom').toBeGreaterThan(100);

  // Another append now surfaces the pill and does NOT move the viewport.
  appendLiveTurn(m);
  await expect(page.locator(PILL), 'pill appears when scrolled up').toBeVisible({ timeout: 12_000 });
  await expect(page.locator(PILL)).toContainText('new');
  const afterPillTop = (await scrollerMetrics(page)).top;
  expect(Math.abs(afterPillTop - scrolledTop), 'viewport did not move on the scrolled-up append').toBeLessThanOrEqual(6);

  // (3) Clicking the pill lands at the bottom.
  await page.locator(PILL).click();
  await settleScroller(page);
  expect((await scrollerMetrics(page)).gap, 'pill click lands at the bottom').toBeLessThan(AT_BOTTOM_SLACK);
});
