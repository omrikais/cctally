import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';
import type { BlockDetail, Envelope } from '../src/types/envelope';

const ENVELOPE = JSON.parse(readFileSync(
  new URL('../../../tests/fixtures/dashboard/tz-override/golden-data.json', import.meta.url), 'utf8',
)) as Envelope;
const DETAIL = JSON.parse(readFileSync(
  new URL('./fixtures/block-timeline-layout.json', import.meta.url), 'utf8',
)) as BlockDetail;

async function serveLayoutFixture(page: Page) {
  // Deliver this synthetic envelope through the real EventSource transport.
  // Reconnects see the same frame, so live fixture ticks cannot replace it.
  await page.addInitScript(() => {
    Object.defineProperty(window, 'SharedWorker', { configurable: true, value: undefined });
  });
  const envelope = {
    ...ENVELOPE,
    generated_at: '2026-04-20T14:00:00Z',
    data_version: 'layout-877-873',
    doctor: null, update: null,
  };
  await page.route('**/api/events', (route) => route.fulfill({
    contentType: 'text/event-stream',
    body: `event: update\ndata: ${JSON.stringify(envelope)}\n\n`,
  }));
  await page.route('**/api/block/*', (route) => route.fulfill({ json: DETAIL }));
  await page.goto('/');
  await page.locator('.source-seg[data-source="claude"]').click();
  await expect(page.locator('#panel-sessions')).toBeVisible();
}

for (const viewport of [{ width: 390, height: 844 }, { width: 1440, height: 900 }]) {
  test(`Sessions controls keep 18px SVG viewports and filter behavior at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await serveLayoutFixture(page);
    const controls = page.locator('#sessions-ctrls');
    await controls.scrollIntoViewIfNeeded();
    for (const id of ['filter-btn', 'sort-pill']) {
      const geometry = await page.locator(`#${id}`).evaluate((button) => {
        const svg = button.querySelector('svg')!;
        return {
          button: button.getBoundingClientRect().toJSON(),
          svg: svg.getBoundingClientRect().toJSON(),
          insideHeader: Boolean(button.closest('.panel-header')),
        };
      });
      await testInfo.attach(`${id}-geometry`, { body: JSON.stringify(geometry), contentType: 'application/json' });
      expect(geometry.svg.width).toBe(18);
      expect(geometry.svg.height).toBe(18);
      expect(geometry.svg.left).toBeGreaterThanOrEqual(geometry.button.left);
      expect(geometry.svg.right).toBeLessThanOrEqual(geometry.button.right);
      expect(geometry.svg.top).toBeGreaterThanOrEqual(geometry.button.top);
      expect(geometry.svg.bottom).toBeLessThanOrEqual(geometry.button.bottom);
      expect(geometry.insideHeader).toBe(viewport.width > 640);
      if (viewport.width <= 640) {
        expect(geometry.button.width).toBeGreaterThanOrEqual(44);
        expect(geometry.button.height).toBeGreaterThanOrEqual(44);
      }
    }
    await testInfo.attach('sessions-controls', { body: await controls.screenshot(), contentType: 'image/png' });
    const rows = page.locator('#panel-sessions tr[data-session-id]');
    await expect(rows).toHaveCount(1);
    await page.locator('#filter-btn').click();
    await expect(page.locator('#filter-input')).toBeFocused();
    await page.locator('#filter-input').fill('no-matching-project');
    await expect(rows).toHaveCount(0);
    await page.locator('#filter-input').press('Enter');
    await expect(page.locator('#filter-btn.as-chip')).toContainText('no-matching-project');
    await page.locator('#filter-btn .chip-x').click();
    await expect(rows).toHaveCount(1);
    await expect(page.locator('#filter-btn svg')).toBeVisible();
    if (viewport.width <= 640) {
      const search = page.locator('#search-container');
      await expect(search).toBeVisible();
      const filterBox = await page.locator('#filter-btn').boundingBox();
      const searchBox = await search.boundingBox();
      expect(searchBox!.y).toBeGreaterThanOrEqual(filterBox!.y + filterBox!.height);
    }
  });

  test(`Block modal keeps every sample above a lower retained total visible at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await serveLayoutFixture(page);
    // Use the real Claude BlocksPanel entry and modal route, not a mounted
    // chart in isolation. Only the detail's accounting payload is synthetic.
    await page.locator('#panel-blocks .blocks-row').first().click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.locator('.mblock-timeline')).toBeVisible();
    await expect(dialog.getByRole('group', { name: 'Total cost: $14.50' })).toBeVisible();
    await expect(dialog.locator('.mblock-timeline-note')).toContainText('totals $17.81');
    const geometry = await dialog.locator('.mblock-timeline svg').evaluate((svg) => {
      const line = svg.querySelector('polyline')! as SVGPolylineElement;
      const points = Array.from({ length: line.points.numberOfItems }, (_, i) => {
        const point = line.points.getItem(i);
        return { x: point.x, y: point.y };
      });
      const circle = svg.querySelector('circle')!;
      return { points, svg: svg.getBoundingClientRect().toJSON(), circle: circle.getBoundingClientRect().toJSON() };
    });
    await testInfo.attach('block-timeline-geometry', { body: JSON.stringify(geometry), contentType: 'application/json' });
    expect(geometry.points.map(({ x }) => Math.round(x * 10) / 10)).toEqual([46, 111.4, 209.5, 307.6, 536.5]);
    for (const { y } of geometry.points) {
      expect(y).toBeGreaterThanOrEqual(18);
      expect(y).toBeLessThanOrEqual(160);
    }
    expect(geometry.points.at(-1)!.y).toBe(18);
    expect(geometry.circle.top).toBeGreaterThanOrEqual(geometry.svg.top);
    expect(geometry.circle.bottom).toBeLessThanOrEqual(geometry.svg.bottom);
    await testInfo.attach('block-modal', { body: await dialog.screenshot(), contentType: 'image/png' });
  });
}
