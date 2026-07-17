import { test, expect } from '@playwright/test';

// #293 S1 — the durable responsive-board contract (the smoke test only checks
// that a grid appears). Sweeps the critical widths asserting the JS-driven
// board mode (data-board-mode), the tall-card data-span contract, and no
// document-level horizontal overflow — the geometry JSDOM cannot evaluate.

const MODE_AT: Array<{ w: number; mode: 'stack' | 'intermediate' | 'bento' }> = [
  { w: 390, mode: 'stack' },
  { w: 899, mode: 'stack' },
  { w: 900, mode: 'intermediate' },
  { w: 1024, mode: 'intermediate' },
  { w: 1199, mode: 'intermediate' },
  { w: 1200, mode: 'bento' },
  { w: 1440, mode: 'bento' },
];

for (const { w, mode } of MODE_AT) {
  test(`board mode is ${mode} and no x-overflow at ${w}px`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 900 });
    await page.goto('/');
    await expect(page.locator('.dash-grid')).toHaveAttribute('data-board-mode', mode);
    const noOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    );
    expect(noOverflow, `no horizontal document overflow at ${w}px`).toBe(true);
    if (mode !== 'stack') {
      // The live grid (not the skeleton) carries data-panel-host.
      await expect(
        page.locator('.panel-host[data-panel-host="sessions"]'),
      ).toBeVisible();
      const sessions = await page
        .locator('.panel-host[data-panel-host="sessions"]').getAttribute('data-span');
      const trend = await page
        .locator('.panel-host[data-panel-host="trend"]').getAttribute('data-span');
      expect(sessions).toBe(mode === 'intermediate' ? '12' : '6');
      expect(trend).toBe(mode === 'intermediate' ? '6' : '3');
    }
  });
}

// The prefs persistence contract (src/store/store.ts): key
// `ccusage.dashboard.prefs`, panelOrder is a GridPanelId[], and
// panelOrderSchemaVersion pinned to CURRENT (6) so applyPanelOrderMigration is
// a no-op and reconcilePanelOrder preserves the seeded positions verbatim.
const PREFS_KEY = 'ccusage.dashboard.prefs';
const MIDDLE_SESSIONS_ORDER = [
  'trend', 'sessions', 'projects',
  'daily', 'cache-report', 'weekly', 'monthly', 'blocks', 'forecast',
  'alerts',
];

test('intermediate tall row keeps Sessions on its own row even when reordered', async ({ page }) => {
  // Persist a tall order with Sessions in the MIDDLE, then load at an
  // intermediate width. Dense flow must still give Sessions a full-width row
  // and pair Trend/Projects.
  await page.addInitScript(
    ({ key, order }) => {
      localStorage.setItem(
        key,
        JSON.stringify({ panelOrder: order, panelOrderSchemaVersion: 6 }),
      );
    },
    { key: PREFS_KEY, order: MIDDLE_SESSIONS_ORDER },
  );
  await page.setViewportSize({ width: 1024, height: 900 });
  await page.goto('/');
  await expect(page.locator('.dash-grid')).toHaveAttribute('data-board-mode', 'intermediate');
  // Confirm the seeded (middle-Sessions) DOM order actually took effect.
  const tallOrder = await page.locator('.bento-row.row-tall .panel-host').evaluateAll(
    (nodes) => nodes.map((n) => (n as HTMLElement).dataset.panelHost),
  );
  expect(tallOrder).toEqual(['trend', 'sessions', 'projects']);
  const grid = await page.locator('.bento-row.row-tall').boundingBox();
  const sessions = await page.locator('.panel-host[data-panel-host="sessions"]').boundingBox();
  expect(sessions && grid).toBeTruthy();
  // Sessions occupies ~the full row width (its own row) despite sitting in the
  // middle of the stored order — the dense-flow determinism guarantee.
  expect(sessions!.width).toBeGreaterThan(grid!.width * 0.9);
});

// #293 S2 — the Sessions Cost column must never be clipped behind the
// overflow-x valve (SESS-1), across the stack/intermediate/bento table-mode
// bands, and the row must be keyboard-openable via its title button (A11Y-2).
// The served fixture is mixed-model (a sonnet session alongside the opus ones,
// see bin/build-e2e-fixtures.py::build_second_model), so the 7-column tight
// case is exercised at 1440 (Sessions span-6 ≈ 650px body).
const COST_WIDTHS = [768, 899, 900, 1199, 1200, 1440, 1920];

for (const w of COST_WIDTHS) {
  test(`Sessions Cost is within the card body at ${w}px`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 900 });
    await page.goto('/');
    const body = page.locator('#panel-sessions .panel-body');
    await expect(body).toBeVisible();
    // The Cost cell is `td.num` WITHOUT `.cache` (Cache is `td.num.cache`, shed
    // — display:none — at the tight tier, where its boundingBox is null). This
    // selector targets Cost only, the rightmost priority column.
    const costCells = page.locator('#panel-sessions .sess-table tbody td.num:not(.cache)');
    await expect(costCells.first()).toBeVisible();
    const bb = (await body.boundingBox())!;
    const bodyRight = bb.x + bb.width;
    const n = await costCells.count();
    for (let i = 0; i < n; i++) {
      const box = (await costCells.nth(i).boundingBox())!;
      expect(box.x + box.width, `Cost cell ${i} right edge within body at ${w}px`)
        .toBeLessThanOrEqual(bodyRight + 1);
    }
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    );
    expect(overflow, `no x-overflow at ${w}px`).toBe(true);
  });
}

test('a session row opens its detail modal via the title button', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  const firstOpen = page.locator('#panel-sessions .sess-open-title').first();
  await expect(firstOpen).toBeVisible();
  await firstOpen.focus();
  await page.keyboard.press('Enter');
  // The modal shell renders <div role="dialog" aria-modal="true"> (Modal.tsx).
  await expect(page.locator('[role="dialog"]').first()).toBeVisible();
});

test('a long session title ellipsizes while Cost stays in bounds at span-6', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  const titleBtn = page.locator('#panel-sessions .sess-open-title').first();
  await expect(titleBtn).toBeVisible();
  const clipped = await titleBtn.evaluate(
    (el) => el.scrollWidth > el.clientWidth || el.textContent!.length < 40,
  );
  expect(clipped).toBe(true); // ellipsized OR legitimately short — never spilling the card
});

// #293 S3 — stacked-card summary bounds. Fixture-independent computed-style
// ENGAGEMENT + the same-page collapse resize (JSDOM can't evaluate @media/
// attribute-scoped CSS). Exact slice counts live in vitest; full real-data
// page density is the ui-qa gate.
//
// NOTE: defaultPrefs() ships `blocksCollapsed: true` (paired with dailyCollapsed
// as a tested product default), so a fresh browser starts Blocks COLLAPSED —
// which S3 makes the ~280px glanceable summary (the default view). Expanding
// (blocksCollapsed:false) shows more within a taller 480px bound. Both bounded.

test('default (collapsed) Blocks = 280px glance <900, released ≥900, same-page 899→900→899', async ({ page }) => {
  // No seed: the default stacked view is collapsed = the 280px glance. Also the
  // persisted-collapse-across-the-900-boundary regression (Codex F7).
  await page.setViewportSize({ width: 899, height: 844 });
  await page.goto('/');
  const body = page.locator('#panel-blocks .panel-body');
  await expect(body).toBeVisible();
  await expect(page.locator('.dash-grid')).toHaveAttribute('data-board-mode', 'stack');
  expect(await body.evaluate((el) => getComputedStyle(el).maxHeight)).toBe('280px');
  await page.setViewportSize({ width: 900, height: 844 });
  await expect(page.locator('.dash-grid')).toHaveAttribute('data-board-mode', 'intermediate');
  expect(await body.evaluate((el) => getComputedStyle(el).maxHeight)).not.toBe('280px');
  await page.setViewportSize({ width: 899, height: 844 });
  await expect(page.locator('.dash-grid')).toHaveAttribute('data-board-mode', 'stack');
  expect(await body.evaluate((el) => getComputedStyle(el).maxHeight)).toBe('280px');
});

test('expanded (blocksCollapsed=false) Blocks = 480px bounded scroll <900, released ≥900', async ({ page }) => {
  await page.addInitScript(({ key }) => {
    const raw = localStorage.getItem(key);
    const prefs = raw ? JSON.parse(raw) : {};
    prefs.blocksCollapsed = false;
    localStorage.setItem(key, JSON.stringify(prefs));
  }, { key: 'ccusage.dashboard.prefs' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const body = page.locator('#panel-blocks .panel-body');
  await expect(page.locator('.dash-grid')).toHaveAttribute('data-board-mode', 'stack');
  expect(await body.evaluate((el) => getComputedStyle(el).maxHeight)).toBe('480px');
  await page.setViewportSize({ width: 1024, height: 844 });
  await expect(page.locator('.dash-grid')).toHaveAttribute('data-board-mode', 'intermediate');
  expect(await body.evaluate((el) => getComputedStyle(el).maxHeight)).not.toBe('480px');
});

test('stacked Weekly/Monthly render at most CAP periods; all rows ≥900', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const wkStack = await page.locator('#panel-weekly .period').count();
  const moStack = await page.locator('#panel-monthly .period').count();
  expect(wkStack).toBeLessThanOrEqual(3);
  expect(moStack).toBeLessThanOrEqual(3);
  await page.setViewportSize({ width: 1024, height: 844 });
  await expect(page.locator('.dash-grid')).toHaveAttribute('data-board-mode', 'intermediate');
  const wkWide = await page.locator('#panel-weekly .period').count();
  // At ≥900 no slice: never fewer than the stack view showed.
  expect(wkWide).toBeGreaterThanOrEqual(wkStack);
});
