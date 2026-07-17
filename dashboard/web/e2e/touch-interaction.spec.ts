import { test, expect } from '@playwright/test';

// #293 S4 — the committed touch/interaction regression net. JSDOM can't evaluate
// @media / pointer:coarse or measure layout, so these live in a real browser:
//   - the off-board coarse-pointer 44px floor (real-box boundingBox ≥44),
//   - BASKET-1: a non-empty basket stays reachable in the condensed mobile
//     header after scroll (the @media display:none a JSDOM test can't see),
//   - SHARE-1: the phone Share modal renders the Live preview before the knobs,
//   - the 320px topbar: the doctor chip compacts (label hidden, status kept) and
//     the document does not x-overflow with a non-empty basket.
// Each targets S4 CSS/markup that the pre-S4 bundle lacks, so it is RED on the
// old bundle (verified by rebuilding the served bundle after the source change).

const BASKET_KEY = 'cctally:share:basket';

// A single valid BasketItem (shape validated by basketSlice.isBasketItemShape)
// seeded into localStorage BEFORE load, so the store hydrates a non-empty basket
// and the BasketChip + composer section list render without any UI walk.
function basketSeed() {
  return [{
    id: 'e2e-basket-1',
    panel: 'weekly',
    template_id: 'weekly-recap',
    options: {
      format: 'md', theme: 'light', reveal_projects: false, no_branding: false,
      top_n: 5, period: { kind: 'current' }, project_allowlist: null,
      show_chart: true, show_table: true,
    },
    added_at: '2026-05-12T09:00:00Z',
    data_digest_at_add: 'sha256:e2e',
    kernel_version: 1,
    label_hint: 'Weekly recap',
  }];
}

async function seedBasket(page: import('@playwright/test').Page) {
  await page.addInitScript(
    ({ key, items }) => localStorage.setItem(key, JSON.stringify(items)),
    { key: BASKET_KEY, items: basketSeed() },
  );
}

async function box(locator: import('@playwright/test').Locator) {
  await expect(locator).toBeVisible();
  const bb = await locator.boundingBox();
  if (!bb) throw new Error('no bounding box');
  return bb;
}

test.describe('#293 S4 — off-board coarse-pointer 44px floor (hasTouch @ 768)', () => {
  test.use({ hasTouch: true, viewport: { width: 768, height: 1024 } });

  test('a hasTouch context reports pointer: coarse', async ({ page }) => {
    await page.goto('/');
    const coarse = await page.evaluate(() => window.matchMedia('(pointer: coarse)').matches);
    expect(coarse).toBe(true);
  });

  test('the panel-modal close button is ≥44×44', async ({ page }) => {
    await page.goto('/');
    await page.locator('#panel-forecast .panel-expand').click();
    const bb = await box(page.locator('.modal-card .modal-close').first());
    expect(bb.width).toBeGreaterThanOrEqual(44);
    expect(bb.height).toBeGreaterThanOrEqual(44);
  });

  test('a modal sort header is ≥44 tall', async ({ page }) => {
    await page.goto('/');
    // Monthly opens the PeriodModal → PeriodTable (SortableHeader); the fixture
    // has monthly rows (all-time calendar rollup), so the header renders (the
    // Trend modal empty-gates when the synthetic fixture has no $/1% history).
    await page.locator('#panel-monthly .panel-expand').click();
    const bb = await box(page.locator('.modal-card .th-sortable .th-sort-btn').first());
    expect(bb.height).toBeGreaterThanOrEqual(44);
  });

  test('composer close / drag-handle / kebab / clear-all / opened menu items are ≥44', async ({ page }) => {
    await seedBasket(page);
    await page.goto('/');
    await page.locator('.basket-chip').click();
    const dialog = page.locator('.composer-modal[role="dialog"]');
    await expect(dialog).toBeVisible();
    for (const sel of [
      '.composer-modal-close',
      '.composer-drag-handle',
      '.composer-section-actions > button',
      '.composer-clear-all',
    ]) {
      const bb = await box(dialog.locator(sel).first());
      expect(bb.width, `${sel} width`).toBeGreaterThanOrEqual(44);
      expect(bb.height, `${sel} height`).toBeGreaterThanOrEqual(44);
    }
    // Open the kebab → its menu items clear the 44px floor too.
    await dialog.locator('.composer-section-actions > button').first().click();
    const bb = await box(dialog.locator('.composer-section-menu button').first());
    expect(bb.height, 'menu item height').toBeGreaterThanOrEqual(44);
  });
});

test.describe('#293 S4 SHARE-1 — phone Share renders preview-first (hasTouch @ 390)', () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 740 } });

  test('the Live preview precedes the knob stack in the DOM', async ({ page }) => {
    await page.goto('/');
    await page.locator('#panel-forecast .share-icon').click();
    const modal = page.locator('.share-modal[role="dialog"]');
    await expect(modal).toBeVisible();
    await expect(modal.locator('.share-preview-col')).toBeVisible();
    await expect(modal.locator('.share-knobs-col')).toBeVisible();
    const previewLeads = await modal.evaluate((root) => {
      const p = root.querySelector('.share-preview-col');
      const k = root.querySelector('.share-knobs-col');
      // DOCUMENT_POSITION_FOLLOWING (4): knobs FOLLOWS preview → preview leads.
      return !!(p && k && (p.compareDocumentPosition(k) & 4));
    });
    expect(previewLeads).toBe(true);
    // Exactly one preview pane (no duplicate render across the branch).
    expect(await modal.locator('.share-preview-col').count()).toBe(1);
  });
});

test.describe('#293 S4 BASKET-1 — basket reachable in the condensed header (hasTouch @ 390)', () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 740 } });

  test('a non-empty basket stays visible when scrolled and opens the composer', async ({ page }) => {
    await seedBasket(page);
    await page.goto('/');
    // Basket chip present before scroll (non-empty basket).
    await expect(page.locator('.basket-chip')).toBeVisible();
    // Scroll the hero out of view so the header condenses (heroScrolled →
    // .topbar.is-scrolled). A trusted wheel over the main content drives the
    // IntersectionObserver.
    await page.locator('#main-content').hover();
    await expect(async () => {
      await page.mouse.wheel(0, 900);
      await expect(page.locator('.topbar.is-scrolled')).toBeVisible({ timeout: 500 });
    }).toPass({ timeout: 8000 });
    // The basket chip is STILL visibly rendered in the condensed header
    // (BASKET-1: removed from the is-scrolled display:none list).
    const chip = page.locator('.basket-chip');
    await expect(chip).toBeVisible();
    expect(await chip.evaluate((el) => getComputedStyle(el).display)).not.toBe('none');
    await chip.click();
    await expect(page.locator('.composer-modal[role="dialog"]')).toBeVisible();
  });
});

test.describe('#293 S4 — 320px topbar: compact doctor chip, no x-overflow (@ 320)', () => {
  test.use({ hasTouch: true, viewport: { width: 320, height: 720 } });

  test('doctor label hides, status stays, and no document x-overflow with a non-empty basket', async ({ page }) => {
    await seedBasket(page);
    await page.goto('/');
    // The doctor aggregate arrives via the SSE snapshot tick.
    await expect(page.locator('.doctor-chip')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('.basket-chip')).toBeVisible();
    // ≤360 compaction: the "Doctor" word is display:none, the status token stays.
    const labelDisplay = await page.locator('.doctor-chip-label')
      .evaluate((el) => getComputedStyle(el).display);
    expect(labelDisplay).toBe('none');
    await expect(page.locator('.doctor-chip-status')).toBeVisible();
    // No document-level horizontal scroll even in the doctor + basket state.
    const noOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    );
    expect(noOverflow, 'no horizontal document overflow at 320px').toBe(true);
  });

  // #293 S4 §4b / §2c — the doctor + basket chips get their coarse-pointer 44px
  // tap band from a `::before` with `inset: -12px 0` / `-10px 0` — grows the hit
  // area VERTICALLY only (0 horizontal). boundingBox() can't see a painted
  // `::before` overhang, so this is proven with document.elementFromPoint: the
  // host owns its vertical band, and the area just past its horizontal edge
  // belongs to the NEIGHBOR — i.e. the vertical-only pseudo reintroduced NO
  // horizontal overhang that would mis-dispatch a neighbor tap.
  //
  // Non-vacuous — how each assertion goes RED:
  //   • *Above* probes (host owns the vertical band): if the `::before` were
  //     dropped or made horizontal-only (e.g. `inset: 0 -8px`), a point 5px
  //     ABOVE the real box would no longer be covered → resolves to the topbar,
  //     not the chip → `*Above` flips false.
  //   • *PastEdge* probes (no horizontal overhang): if the `::before` regained a
  //     horizontal overhang (e.g. `inset: -12px -8px`), a point 3px PAST the
  //     chip's edge toward its neighbor would fall inside that overhang →
  //     resolves back to the host chip → `docPastRight` / `basPastLeft` flip
  //     true. Verified locally by temporarily setting the inset to `-12px -8px`.
  test('the chip ::before hit-band is vertical-only — no horizontal overhang onto a neighbor', async ({ page }) => {
    await seedBasket(page);
    await page.goto('/');
    await expect(page.locator('.doctor-chip')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('.basket-chip')).toBeVisible();
    // The pseudo hit-expansion only exists under pointer: coarse (§2c).
    expect(await page.evaluate(() => window.matchMedia('(pointer: coarse)').matches)).toBe(true);

    const probes = await page.evaluate(() => {
      const ownedBy = (x: number, y: number, cls: string) => {
        const el = document.elementFromPoint(x, y) as Element | null;
        return !!(el && el.closest(cls));
      };
      const doc = document.querySelector('.doctor-chip')!.getBoundingClientRect();
      const bas = document.querySelector('.basket-chip')!.getBoundingClientRect();
      return {
        // (a) the chip's center vertical band resolves to that chip (or a child).
        docCenter: ownedBy(doc.left + doc.width / 2, doc.top + doc.height / 2, '.doctor-chip'),
        basCenter: ownedBy(bas.left + bas.width / 2, bas.top + bas.height / 2, '.basket-chip'),
        // a point 5px ABOVE the real box (inside the -12/-10 vertical ::before)
        // is still owned by the host — proves the vertical hit-band is real.
        docAbove: ownedBy(doc.left + doc.width / 2, doc.top - 5, '.doctor-chip'),
        basAbove: ownedBy(bas.left + bas.width / 2, bas.top - 5, '.basket-chip'),
        // (b) 3px PAST the horizontal edge toward the neighbor is NOT the host —
        // doctor faces basket (right), basket faces doctor (left).
        docPastRight: ownedBy(doc.right + 3, doc.top + doc.height / 2, '.doctor-chip'),
        basPastLeft: ownedBy(bas.left - 3, bas.top + bas.height / 2, '.basket-chip'),
      };
    });

    expect(probes.docCenter, 'doctor center resolves to the doctor chip').toBe(true);
    expect(probes.basCenter, 'basket center resolves to the basket chip').toBe(true);
    expect(probes.docAbove, 'doctor vertical ::before band is host-owned').toBe(true);
    expect(probes.basAbove, 'basket vertical ::before band is host-owned').toBe(true);
    expect(probes.docPastRight, 'no doctor ::before overhang onto its right neighbor').toBe(false);
    expect(probes.basPastLeft, 'no basket ::before overhang onto its left neighbor').toBe(false);
  });
});
