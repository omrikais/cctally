import { test, expect } from '@playwright/test';
import {
  loadManifest, openConversation, settleScroller, scrollerMetrics,
  turnVisibleInReader, armFlashWatch, flashWasSeen, uuidSel, READER_BODY,
  AT_BOTTOM_SLACK,
} from './utils';

// Scenario 7 (spec §4-B2, r3/r4) — the ABOVE-insertion reveal-pin guard. A
// find-jump to a late member of a giant windowed sidechain (member 198 of 260,
// window cap 150 ⇒ centeredWindow clamps win.start to 110) force-opens the card
// and renders a "Show 100 earlier" control above the window. Clicking it inserts
// 100 members ABOVE the held anchor (window start = member 110). The #239
// convergent reassert (SidechainGroup's captureReanchor + reassertCenter) must
// hold that anchor at its captured viewport top while the content grows above it —
// the ORIGINAL, real above-insertion bug #239 fixed and still owns.
//
// This replaces the r2 fixme'd BELOW-insertion ("Show all") test, which was both
// structurally vacuous as a reassert guard AND flagged a phantom ~24k px "fling"
// that real-browser instrumentation traced to a `locator.click()` actionability
// auto-scroll artifact (the anchor was captured PRE-actionability, then compared
// against the product's POST-actionability position). The fix here: run the
// actionability scroll FIRST (`scrollIntoViewIfNeeded`) and settle BEFORE
// capturing the anchor, so the pre/post comparison is honest.
//
// RED lever (recorded in docs/superpowers/plans/2026-07-11-281-s5-red-evidence.md):
// the FULL NO-OP of the reveal correction (the reassert never applies) reproduces
// the exact historical fling — `anchor drift 16393px`. The r3 one-shot lever was
// proven un-fireable on this synchronous plain-DOM geometry (it PASSES 3× stable),
// so it is NOT the lever; the no-op is.
//
// The former yank on a COLLAPSED-card find-jump (the SIZE_INCREASED firing the
// LIVE `followOutput` watcher) is now FIXED (#291), and the viewport-position
// assertion for that geometry is owned by the "does not yank to the bottom (#291)"
// scenario below in this file. This scenario's purpose is the reveal-ANCHOR guard,
// not viewport position, so it still asserts only the flash (the load-bearing
// "landed on 198" signal). We keep the `scrollIntoViewIfNeeded` normalization on
// the reveal control before capturing the anchor — its job is the INDEPENDENT
// Playwright `click()` actionability-scroll artifact documented above, unrelated
// to the (now-fixed) yank.
const m = loadManifest();
const FINDBAR = '.conv-findbar';
const FIND_INPUT = '.conv-findbar-input';

test('revealing earlier members holds the anchor while content inserts above (#285 pt2, B2)', async ({ page }) => {
  test.setTimeout(60_000);
  await openConversation(page, m.sidechain_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);

  // Arm the flash watch on the late member BEFORE the jump (robust to the ~2s
  // pulse and to the member not yet being mounted — the observer latches on the
  // class mutation once the card force-opens).
  await armFlashWatch(page, m.reveal_late_member_uuid);

  // Find-jump to the unique needle at member 198. The jump force-opens the
  // collapsed giant card and centers its internal window on member 198
  // (centeredWindow(260, 198, 150) ⇒ start 110), so a "Show 100 earlier" control
  // renders above the 110..259 window.
  await page.locator(READER_BODY).click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('/');
  await expect(page.locator(FINDBAR)).toBeVisible();
  await page.locator(FIND_INPUT).fill(m.reveal_late_needle);
  await expect(page.locator('.conv-findbar-count')).toContainText('1 / 1');
  await page.locator(FIND_INPUT).press('Enter');

  // The jump landed/pinned the target — the flash is the load-bearing signal for
  // THIS reveal-anchor guard (the viewport-position guarantee for this geometry is
  // owned by the #291 scenario below). `toBeVisible` is a DOM-mounted check, not an
  // in-viewport one.
  await expect(page.locator(uuidSel(m.reveal_late_member_uuid)),
    'the late member mounted (the card force-opened)').toBeVisible({ timeout: 15_000 });
  await settleScroller(page);
  expect(await flashWasSeen(page), 'the jump flashed the late member (landed/pinned)').toBe(true);

  // The windowed card renders the "Show 100 earlier" control above the window.
  const showEarlier = page.locator('.conv-window-reveal-bar--before button', { hasText: 'Show 100 earlier' });
  await expect(showEarlier, 'a windowed giant card renders a "Show 100 earlier" control').toBeVisible();

  // Actionability scroll FIRST (spec §4-B2(a)): bring the reveal control into view
  // and settle BEFORE capturing the anchor, so we compare the product's real
  // pre/post positions — not a click() auto-scroll artifact.
  await showEarlier.scrollIntoViewIfNeeded();
  await settleScroller(page);

  // Capture the retained first-window member (the first sidechain member in the
  // DOM is window start = member 110), its viewport top, the scroller scrollTop,
  // and the rendered member count.
  const before = await page.evaluate((sel) => {
    const scroller = document.querySelector(sel) as HTMLElement | null;
    const first = document.querySelector('.conv-sidechain [data-uuid]') as HTMLElement | null;
    const count = document.querySelectorAll('.conv-sidechain [data-uuid]').length;
    return first && scroller
      ? {
          uuid: first.getAttribute('data-uuid'),
          top: Math.round(first.getBoundingClientRect().top),
          scrollTop: Math.round(scroller.scrollTop),
          count,
        }
      : null;
  }, READER_BODY);
  expect(before, 'captured the first-window anchor member').not.toBeNull();
  expect(await turnVisibleInReader(page, before!.uuid!),
    'the captured anchor member intersects the reader viewport').toBe(true);

  // Reveal the 100 members ABOVE the window (members 10..109).
  await showEarlier.click();
  await settleScroller(page, READER_BODY, { anchorSel: uuidSel(before!.uuid!) });

  const after = await page.evaluate(
    ({ uuid, sel }) => {
      const el = document.querySelector(`[data-uuid="${uuid}"]`) as HTMLElement | null;
      const scroller = document.querySelector(sel) as HTMLElement | null;
      const count = document.querySelectorAll('.conv-sidechain [data-uuid]').length;
      return {
        top: el ? Math.round(el.getBoundingClientRect().top) : null,
        scrollTop: scroller ? Math.round(scroller.scrollTop) : null,
        count,
      };
    },
    { uuid: before!.uuid, sel: READER_BODY },
  );

  // (1) Exactly 100 members inserted (window 110..259 → 10..259).
  expect(after.count - before!.count, 'the reveal inserted exactly 100 earlier members').toBe(100);
  // (2) The SAME anchor member is still mounted…
  expect(after.top, 'the anchor member is still mounted after the reveal').not.toBeNull();
  // (3) …held at its captured viewport top (the #239 convergent reassert pinned it).
  const drift = Math.abs((after.top as number) - before!.top);
  expect(drift, `anchor drift ${drift}px after the above-insertion reveal`).toBeLessThanOrEqual(8);
  // (4) Real content inserted above → the scroller advanced materially.
  expect((after.scrollTop as number) - before!.scrollTop,
    'scrollTop rose as 100 members inserted above the held anchor').toBeGreaterThan(200);
});

// Scenario 10 (#291 item 1) — a find-jump into a windowed-out member of a
// COLLAPSED giant subagent card force-opens the card; the card's SIZE_INCREASED
// must NOT yank the viewport to the bottom. Pre-fix this was the KNOWN ISSUE
// scenario 7's header documents (raw-truthy followOutput arms virtuoso's
// resize-autoscroll-to-LAST watcher). The fix: an active same-session
// conversationJump forces followOutput={false} for the jump's lifetime, so the
// watcher never arms. RED lever (docs/superpowers/plans/2026-07-12-291-red-evidence.md):
// drop the `|| activeJumpForSession` term in the reader's followOutput prop → the
// force-open yanks to the bottom (gap ≈ 0, target scrolled out) → RED.
//
// Realistic find flow, NO artificial pause: the `1 / 1` count-wait naturally
// exceeds virtuoso's 100ms watcher window, so the find-typing same-count-refresh
// watcher has expired by the time Enter fires (spec §4 pre-armed-watcher residual).
test('a find-jump into a windowed-out member of a collapsed giant card does not yank to the bottom (#291)', async ({ page }) => {
  test.setTimeout(60_000);
  await openConversation(page, m.sidechain_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);

  await armFlashWatch(page, m.reveal_late_member_uuid);

  await page.locator(READER_BODY).click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('/');
  await expect(page.locator(FINDBAR)).toBeVisible();
  await page.locator(FIND_INPUT).fill(m.reveal_late_needle);
  await expect(page.locator('.conv-findbar-count')).toContainText('1 / 1');
  await page.locator(FIND_INPUT).press('Enter');

  await expect(page.locator(uuidSel(m.reveal_late_member_uuid)),
    'the late member mounted (the card force-opened)').toBeVisible({ timeout: 15_000 });
  await settleScroller(page, READER_BODY, { anchorSel: uuidSel(m.reveal_late_member_uuid) });

  // The three-part guarantee (parallel to reader-jump scenario 8):
  expect(await turnVisibleInReader(page, m.reveal_late_member_uuid),
    'landed on the target, not scrolled out').toBe(true);
  expect((await scrollerMetrics(page)).gap,
    'not yanked to the bottom').toBeGreaterThan(AT_BOTTOM_SLACK);
  expect(await flashWasSeen(page), 'the jump flashed/pinned the target').toBe(true);
});
