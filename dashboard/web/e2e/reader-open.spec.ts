import { test, expect } from '@playwright/test';
import {
  loadManifest, openConversation, settleScroller, scrollerMetrics,
  turnVisibleInReader, READER_BODY, AT_BOTTOM_SLACK,
} from './utils';

// Scenario 1 (spec §4.1 + §5) — the #217/#285 open-position contract, restored by
// B1 (#281 S5). ONE scenario, both halves: a MULTI-PAGE tail open lands at the
// BOTTOM (final turn visible, live-tail engaged); a SINGLE-PAGE conversation opens
// at the TOP (reads from the start).
//
// The single-page-top half is the B1 fix's e2e guard. At HEAD (pre-fix, tracked as
// issue #285) react-virtuoso 4.18.7's raw-truthy `followOutput` prop installs a
// resize-autoscroll-to-LAST watcher that pulls even single-page opens to the
// bottom, so the openScrollIntent 'top' lander's scrollToIndex({index:0}) is
// inert. B1 passes the LITERAL `followOutput={false}` prop while the machine's
// follow-suspension is active (a 'top' landing), which uninstalls that watcher so
// the top landing sticks. RED against the pre-fix bundle: the single-page open
// lands at the bottom (scrollTop large, the first turn off-screen).
const m = loadManifest();

test('open-position contract: multi-page opens at the bottom, single-page opens at the top (#285 B1)', async ({ page }) => {
  // ── (A) multi-page tail open → BOTTOM (unchanged guarantee) ────────────────
  await openConversation(page, m.long_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);

  const long = await scrollerMetrics(page);
  // Genuinely overflowing (else "at bottom" is vacuous).
  expect(long.height - long.client).toBeGreaterThan(1000);
  // Parked at the tail (the scrollHeight estimate is accurate at the bottom).
  expect(long.gap).toBeLessThan(AT_BOTTOM_SLACK);
  expect(long.top).toBeGreaterThan(long.height - long.client - AT_BOTTOM_SLACK);
  // The conversation's FINAL turn is mounted and visible — the load-bearing
  // "opened at the bottom, showing the end" guarantee.
  expect(await turnVisibleInReader(page, m.long_last_uuid),
    'multi-page: the final turn is visible at the bottom').toBe(true);

  // ── (B) single-page open → TOP (the B1 fix; RED at pre-fix HEAD) ───────────
  await openConversation(page, m.single_page_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);

  const single = await scrollerMetrics(page);
  // Non-vacuity: the single page genuinely overflows the scroller, so "at the
  // top" is meaningfully distinct from "at the bottom" (else scrollTop 0 == bottom).
  expect(single.height - single.client, 'the single page overflows the viewport').toBeGreaterThan(200);
  // Parked at the TOP (the openScrollIntent 'top' landing stuck). RED at HEAD:
  // the raw-truthy followOutput watcher pulls this to the bottom (scrollTop large).
  expect(single.top, 'the single-page open is parked at the top').toBeLessThanOrEqual(4);
  // …and the FIRST turn is visible in the viewport (reads from the start).
  expect(await turnVisibleInReader(page, m.single_first_uuid),
    'single-page: the first turn is visible at the top').toBe(true);
});
