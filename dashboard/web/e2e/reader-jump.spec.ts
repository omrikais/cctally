import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import {
  loadManifest, openConversation, settleScroller, scrollerMetrics,
  turnVisibleInReader, armFlashWatch, flashWasSeen, uuidSel, wheelUpUntil,
  READER_BODY, AT_BOTTOM_SLACK,
} from './utils';

const m = loadManifest();
const FINDBAR = '.conv-findbar';
const FIND_INPUT = '.conv-findbar-input';
const PILL = '.conv-new-pill';

// Open the find bar and jump to the single match for `needle`.
//
// The jump goes through loadToTarget, which needs the FULL-SESSION outline
// skeleton to decide a backward (head-ward) direction toward an above-window
// target — without it, the no-outline fallback forward-drains from the tail and
// can never reach an earlier turn. So wait for the outline to load first.
async function findJump(page: Page, needle: string) {
  await expect(page.locator('.conv-outline-entry').first(),
    'the full-session outline must be loaded before a backward jump').toBeVisible({ timeout: 15_000 });
  await page.locator(READER_BODY).click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('/');
  await expect(page.locator(FINDBAR)).toBeVisible();
  await page.locator(FIND_INPUT).fill(needle);
  await expect(page.locator('.conv-findbar-count')).toContainText('1 / 1');
  await page.locator(FIND_INPUT).press('Enter');
}

// Scenario 4 (spec §4.4 + §4-B3) — cold-tail jump to an unmounted early turn. Open
// at the tail (fully paged at the bottom, hasMore=false) and jump DIRECTLY to an
// early turn far outside the loaded window: the walk-to-mount + direct-scroll
// pipeline lands the target inside the viewport with the flash pin. The interleaved
// giant rows between the tail and the target make height-estimation drift real.
//
// #286 B3 — the pre-#234 pre-page workaround (a bounded wheel-up that paged one
// reverse page BEFORE jumping) is GONE: this now tests the direct cold-tail
// backward jump, the exact race the exhaustion-clear fix targets (a backward drain
// returns as its top cursor exhausts, potentially BEFORE React commits the
// bringing-prepend, which pre-B3 raced the `!hasMore` clear against the drain
// re-fire and stranded the jump). RED lever (RED-evidence ledger): restore the
// immediate no-hit clear (drop the committed-window-epoch gate) — the cold-tail
// jump then flakes/strands (the vitest resolveExhaustion + runner REDs are the
// deterministic proof).
test('a cold-tail jump to an early turn lands it in the viewport with the flash (#286)', async ({ page }) => {
  // Honest slow work: a cold backward drain over the giant rows — widen past 30s.
  test.setTimeout(60_000);
  await openConversation(page, m.long_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);
  // Precondition: opened parked at the tail, and the early target is NOT loaded.
  expect((await scrollerMetrics(page)).gap, 'opened parked at the tail').toBeLessThan(AT_BOTTOM_SLACK);
  expect(await turnVisibleInReader(page, m.jump_target_uuid), 'target not visible before the jump').toBe(false);

  await armFlashWatch(page, m.jump_target_uuid);
  await findJump(page, m.jump_target_needle);

  // The durable guarantee: after the jump settles the target turn is inside the
  // reader viewport (pre-B3 the exhaustion-clear race strands it entirely → RED).
  await expect(page.locator(uuidSel(m.jump_target_uuid))).toBeVisible({ timeout: 15_000 });
  await settleScroller(page, READER_BODY, { anchorSel: uuidSel(m.jump_target_uuid) });
  expect(await turnVisibleInReader(page, m.jump_target_uuid), 'target visible after the jump').toBe(true);
  // …and we're no longer parked at the bottom (a real jump moved the viewport).
  expect((await scrollerMetrics(page)).gap, 'jumped away from the tail').toBeGreaterThan(AT_BOTTOM_SLACK);
  // The flash pin landed on the target (captured non-racily by the observer).
  expect(await flashWasSeen(page), 'the target flashed on landing').toBe(true);
});

// Scenario 8 (spec §4.8 + §5/F10) — a jump-driven prepend doesn't bump the pill.
// With the reader deliberately scrolled AWAY from the bottom (a BOUNDED wheel-up
// that fires NO reverse page yet, so the pill guard is non-vacuous — a pill can
// only surface when not at bottom), a backward jump-prepend (loadToTarget's
// prepend path) must neither surface "↓ N new" nor scroll to the bottom. RED lever
// (pill half): remove the `lastOp.op !== 'append'` early-return in the pill effect
// (treat every window op as an append) so the jump-driven prepend bumps it.
test('a jump-driven backward prepend does not bump the pill or scroll to bottom (#286)', async ({ page }) => {
  test.setTimeout(60_000);
  // Count reverse-page (`?before=`) requests across the whole run.
  let beforeCount = 0;
  const onReq = (r: { url(): string }) => { if (/[?&]before=/.test(r.url())) beforeCount += 1; };
  page.on('request', onReq);
  try {
    await openConversation(page, m.long_session_id);
    await expect(page.locator(READER_BODY)).toBeVisible();
    await settleScroller(page);

    // Bounded wheel-up: leave the bottom (gap > AT_BOTTOM_SLACK) but stay INSIDE the
    // mounted window so NO reverse page fires yet — this is what makes the pill guard
    // meaningful (a pill can't surface while parked at the bottom). `wheelUpUntil`
    // wheels the MINIMUM (a small ~800px step) to clear the ~100px at-bottom slack —
    // fixture-height-independent, and the small step stays below the (page-tall) head
    // so no reverse page fires.
    const baseline = beforeCount;
    const offBottom = await wheelUpUntil(
      page,
      async () => (await scrollerMetrics(page)).gap > AT_BOTTOM_SLACK,
      { stepPx: 800 },
    );
    expect(offBottom, 'a bounded wheel-up cleared the at-bottom slack').toBe(true);
    expect((await scrollerMetrics(page)).gap, 'scrolled up off the bottom').toBeGreaterThan(AT_BOTTOM_SLACK);
    expect(beforeCount - baseline, 'the bounded wheel-up fired no reverse page yet').toBe(0);
    await expect(page.locator(PILL), 'no pill before the jump').toHaveCount(0);

    // The cold backward jump: loadToTarget drains head-ward (≥1 `?before=`), a
    // prepend that must NOT bump the pill nor yank the viewport to the bottom.
    const jumpBaseline = beforeCount;
    await findJump(page, m.jump_target_needle);
    await expect(page.locator(uuidSel(m.jump_target_uuid))).toBeVisible({ timeout: 15_000 });
    await settleScroller(page, READER_BODY, { anchorSel: uuidSel(m.jump_target_uuid) });

    // The backward jump genuinely drained at least one reverse page (non-vacuity).
    expect(beforeCount - jumpBaseline, 'the backward jump drained ≥1 reverse page').toBeGreaterThanOrEqual(1);
    // The pill never surfaced (the jump-prepend is NOT a live append)…
    await expect(page.locator(PILL), 'jump-prepend must not bump the pill').toHaveCount(0);
    // …and the viewport landed on the target, NOT yanked to the bottom.
    expect(await turnVisibleInReader(page, m.jump_target_uuid), 'landed on the target').toBe(true);
    expect((await scrollerMetrics(page)).gap, 'not yanked to the bottom').toBeGreaterThan(AT_BOTTOM_SLACK);
  } finally {
    page.off('request', onReq);
  }
});

// Scenario 9 (spec §4-B4 / Codex F11, #287) — cold-tail jump to a turn JUST BELOW
// the giant band. Target index 580 sits just after the 60–560 giant band and below
// 600, so the reverse-drain approach loads + MEASURES the giant rows above it: the
// walk lands the target amid REAL height-estimation drift. RED lever (RED-evidence
// ledger): replace the walk with a bare single-hop `scrollToIndex(target,'start')`
// — this scenario then strands the target off-screen (the un-measured giant band
// above it mis-estimates the scroll offset), while the item-40 target (at the top
// of the transcript, giant-free above it) would still land — proving the walk is
// load-bearing exactly where the geometry carries measured giants.
test('a cold-tail jump to a turn below the giant band lands it in the viewport with the flash (#287)', async ({ page }) => {
  test.setTimeout(60_000);
  await openConversation(page, m.long_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);
  expect((await scrollerMetrics(page)).gap, 'opened parked at the tail').toBeLessThan(AT_BOTTOM_SLACK);
  expect(await turnVisibleInReader(page, m.below_giants_jump_target_uuid),
    'target not visible before the jump').toBe(false);

  await armFlashWatch(page, m.below_giants_jump_target_uuid);
  await findJump(page, m.below_giants_jump_target_needle);

  await expect(page.locator(uuidSel(m.below_giants_jump_target_uuid))).toBeVisible({ timeout: 15_000 });
  await settleScroller(page, READER_BODY, { anchorSel: uuidSel(m.below_giants_jump_target_uuid) });
  // Landed inside the viewport (a bare scrollToIndex would strand it amid the giant
  // rows → RED), away from the tail, with the flash pin.
  expect(await turnVisibleInReader(page, m.below_giants_jump_target_uuid),
    'target visible after the jump').toBe(true);
  expect((await scrollerMetrics(page)).gap, 'jumped away from the tail').toBeGreaterThan(AT_BOTTOM_SLACK);
  expect(await flashWasSeen(page), 'the target flashed on landing').toBe(true);
});
