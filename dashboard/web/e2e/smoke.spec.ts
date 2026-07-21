import { test, expect } from '@playwright/test';
import { loadManifest, openConversation, settleScroller, READER_BODY } from './utils';

// #281 S3 — the end-to-end smoke: builder → cache pre-prime → dashboard server →
// browser. If this passes the whole harness (isolation env, launcher, port,
// fixture ingest) is wired; the reader scenario specs build on it.
const manifest = loadManifest();

test('dashboard renders its panel grid', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#main-content')).toBeVisible();
  // At least one dashboard panel mounted (the exact panel set is config-driven).
  await expect(page.locator('#main-content .panel-host').first()).toBeVisible();
});

test('conversations rail lists the six fixture conversations', async ({ page }) => {
  // #293 S2 added a fifth fixture — a minimal second-model (sonnet) session —
  // so the served Sessions table is mixed-model (Model column present) for the
  // 7-column SESS-1 tight case. #321 added the sixth Claude identity seed used
  // by the mixed-source reader coverage.
  await page.goto('/#/conversations');
  await expect(page.locator('.conv-rail')).toBeVisible();
  await expect(page.locator('.conv-rail-row')).toHaveCount(6);
});

test('opening the long conversation mounts the reader with content', async ({ page }) => {
  await openConversation(page, manifest.long_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);
  // Virtualized items are present (a tail page's worth mounted somewhere).
  await expect(page.locator(`${READER_BODY} .conv-reader-item`).first()).toBeVisible();
});
