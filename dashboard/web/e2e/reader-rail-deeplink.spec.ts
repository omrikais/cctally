import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { loadManifest } from './utils';

const manifest = loadManifest();

type BrowseRow = { conversation_key: string; title: string | null };

async function codexRows(page: Page): Promise<BrowseRow[]> {
  const rows: BrowseRow[] = [];
  let cursor: string | null = null;
  do {
    const params = new URLSearchParams({ source: 'codex', limit: '50' });
    if (cursor !== null) params.set('cursor', cursor);
    const response = await page.request.get(`/api/conversations?${params}`);
    expect(response.ok()).toBe(true);
    const body = await response.json() as {
      status: string;
      rows: BrowseRow[];
      page: { cursor?: string | null };
    };
    expect(body.status).toBe('ok');
    rows.push(...body.rows);
    cursor = body.page.cursor ?? null;
  } while (cursor !== null);
  return rows;
}

function rowByTitle(rows: BrowseRow[], title: string): BrowseRow {
  const row = rows.find((candidate) => candidate.title === title);
  expect(row, `missing browse row ${title}; got ${rows.length}: ${rows.map((candidate) => candidate.title).join(' | ')}`).toBeTruthy();
  return row!;
}

test('#501 — an out-of-page permalink pins exactly one current rail row', async ({ page }) => {
  const rows = await codexRows(page);
  const outside = rowByTitle(rows, manifest.rail_page_outside_title);
  const inside = rowByTitle(rows, manifest.rail_page_inside_title);
  expect(rows.indexOf(outside)).toBeGreaterThanOrEqual(50);
  expect(rows.indexOf(inside)).toBeLessThan(50);

  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();

  // Seed both rail axes a fresh permalink promises to clear. The project filter
  // is intentionally unrelated to the target, so a stale filter would exclude
  // it even if the selected-row read were correct.
  await page.getByRole('button', { name: /Filters/ }).click();
  await page.getByRole('dialog', { name: 'Conversation filters' }).getByRole('checkbox').first().check();
  await page.getByRole('button', { name: 'Done', exact: true }).click();
  await page.locator('.conv-rail-search input').fill('unrelated search');

  await page.evaluate((key) => { window.location.hash = `#/conversations/${encodeURIComponent(key)}`; }, outside.conversation_key);
  await expect(page.locator('.conv-reader-title')).toHaveText(manifest.rail_page_outside_title);
  await expect(page.locator('.conv-rail-search input')).toHaveValue('');
  await expect(page.locator('.conv-rail-filters-activechip')).toHaveCount(0);
  const active = page.locator('.conv-rail-row.is-active');
  await expect(active).toHaveCount(1);
  await expect(active).toContainText(manifest.rail_page_outside_title);

  // The already-loaded control keeps the same one-row current contract.
  await page.evaluate((key) => { window.location.hash = `#/conversations/${encodeURIComponent(key)}`; }, inside.conversation_key);
  await expect(page.locator('.conv-reader-title')).toHaveText(manifest.rail_page_inside_title);
  await expect(page.locator('.conv-rail-row.is-active')).toHaveCount(1);
  await expect(page.locator('.conv-rail-row.is-active')).toContainText(manifest.rail_page_inside_title);
});
