import { test, expect } from '@playwright/test';
import { loadManifest, openConversation, settleScroller, uuidSel, wheelUpUntil, READER_BODY } from './utils';

// Scenario 2 (spec §4.2) — reverse-prepend anchor stability. From the bottom,
// scroll (trusted wheel) toward the virtual top of the loaded window until
// Virtuoso's `startReached` fires the `?before=` page; after the prepend commits
// (the atomic `firstItemIndex` adjustment), a previously-visible turn's viewport
// rect.top stays put (±2px). RED lever: skew the `firstItemIndex` delta (off-by-N)
// so the anchor slips on prepend.
const m = loadManifest();

test('a reverse-page prepend keeps the anchored turn pinned', async ({ page }) => {
  // Honest slow work: the scroll-up must traverse the whole (prefetch-enlarged)
  // mounted window up to its head to fire the held `?before=` page — measured
  // ~12–20s on the ubuntu-latest CI runner. Widen past the 30s default.
  test.setTimeout(75_000);
  await openConversation(page, m.long_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);

  // Gate the first `?before=` page so we can snapshot the anchor between the head
  // mounting and the prepend committing. The route handler flags arrival then
  // holds until we `release` after the snapshot — condition-based, no timer.
  let seenFlag = false;
  let markSeen: () => void = () => {};
  const seen = new Promise<void>((r) => { markSeen = r; });
  let release: () => void = () => {};
  const held = new Promise<void>((r) => { release = r; });
  let intercepted = 0;
  await page.route(/\/api\/conversation\/.*before=/, async (route) => {
    if (intercepted++ === 0) { seenFlag = true; markSeen(); await held; }
    await route.continue();
  });

  // A direct scrollTop write is unreliable on Virtuoso's own scroller; scroll with
  // trusted wheel events until the head scrolls in and `startReached` fires the
  // (now-held) `?before=` page. The step COUNT is budgeted, not fixed: the mounted
  // window can be a full extra page tall (an initial-mount reverse prefetch), so a
  // fixed count that reaches the head locally falls short on a slow CI renderer.
  const fired = await wheelUpUntil(page, () => seenFlag);
  expect(fired, 'a reverse `?before=` page fired during scroll-up').toBe(true);
  await seen;
  await settleScroller(page); // head mounted at the wheel-stop, prepend held

  // Snapshot the topmost fully-visible turn (the anchor).
  const anchor = await page.evaluate((sel) => {
    const b = document.querySelector(sel) as HTMLElement;
    const br = b.getBoundingClientRect();
    let best: { uuid: string; top: number } | null = null;
    for (const el of Array.from(document.querySelectorAll('[data-uuid]')) as HTMLElement[]) {
      const uuid = el.getAttribute('data-uuid');
      if (!uuid) continue;
      const r = el.getBoundingClientRect();
      if (r.bottom > br.top + 4 && r.top >= br.top - 1) {
        if (!best || r.top < best.top) best = { uuid, top: Math.round(r.top) };
      }
    }
    return best;
  }, READER_BODY);
  expect(anchor, 'a visible anchor turn at the head of the loaded window').not.toBeNull();
  const beforeScrollTop = await page.evaluate((s) => Math.round((document.querySelector(s) as HTMLElement).scrollTop), READER_BODY);

  // Release the prepend; wait for the response + the anchor-tier settle (mounted
  // range AND the anchor's rect must both stabilize, not just scrollTop).
  const beforeResp = page.waitForResponse(/before=/);
  release();
  await beforeResp;
  await settleScroller(page, READER_BODY, { anchorSel: uuidSel(anchor!.uuid) });

  const after = await page.evaluate(({ sel, uuid }) => {
    const b = document.querySelector(sel) as HTMLElement;
    const el = document.querySelector(`[data-uuid="${uuid}"]`) as HTMLElement | null;
    return { top: el ? Math.round(el.getBoundingClientRect().top) : null, scrollTop: Math.round(b.scrollTop) };
  }, { sel: READER_BODY, uuid: anchor!.uuid });

  // Non-vacuity: a whole page (~500 rows, ~18000px) was inserted above the anchor.
  // WITHOUT the firstItemIndex pin the anchor would jump by that entire height; the
  // pin keeps its viewport top within a small residual.
  const prependedPx = after.scrollTop - beforeScrollTop;
  expect(prependedPx, 'a full page was prepended above the anchor').toBeGreaterThan(15000);

  // The anchored turn's viewport top barely moved (the firstItemIndex pin). The
  // residual is react-virtuoso's estimated-height imprecision over the ~500
  // above-the-fold prepended rows (measured ~36px, i.e. <0.2% of the prependedPx
  // it would otherwise slip). The RED lever (skewing the firstItemIndex delta
  // off-by-N) slips it by N rows (~36px each) — a large multiple of this residual.
  expect(after.top).not.toBeNull();
  const drift = Math.abs((after.top as number) - anchor!.top);
  expect(drift, `anchor drift ${drift}px vs ${prependedPx}px prepended`).toBeLessThanOrEqual(60);
});
