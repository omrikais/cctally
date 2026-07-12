import { test, expect } from '@playwright/test';
import { loadManifest, openConversation, settleScroller, READER_BODY } from './utils';

// #289 — Escape peels ONE layer at a time. With a reader open and nothing else
// active, the FIRST Escape deselects the conversation back to the LIST (URL
// `#/conversations`, reader torn down, rail still visible) — NOT the dashboard.
// A SECOND Escape from the list leaves the workspace to the dashboard. The old
// single-step behavior ejected straight to the dashboard on one keystroke (QA
// hit it twice as a surprise).
//
// RED lever: revert the ConversationsView Escape `action` (drop the two new peel
// branches) and the first Escape lands on the dashboard (no `#/conversations`,
// the rail gone), failing the first-Escape assertions.
const m = loadManifest();

test('Escape steps back one level: reader → conversations list → dashboard', async ({ page }) => {
  await openConversation(page, m.long_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);

  // Sanity: we opened the reader at the session route.
  const sidHash = `#/conversations/${encodeURIComponent(m.long_session_id)}`;
  expect(page.url(), 'reader open at the session route').toContain(sidHash);

  // Ensure no rail/find input holds focus (inputMode must be null for the global
  // Escape binding to fire): click into the reader body first.
  await page.locator(READER_BODY).click({ position: { x: 5, y: 5 } });

  // ── FIRST Escape → deselect to the conversations LIST (not the dashboard) ────
  await page.keyboard.press('Escape');

  // Reader torn down for this session; the empty pane + rail remain.
  await expect(page.locator(READER_BODY)).toHaveCount(0);
  await expect(page.locator('.conv-reader--empty')).toBeVisible();
  await expect(page.locator('.conv-rail')).toBeVisible();
  // Still in the conversations workspace — NOT the dashboard grid.
  await expect(page.locator('.conv-view')).toBeVisible();
  await expect(page.locator('.panel-host')).toHaveCount(0);
  // URL is the bare list route (no sid) — discriminates deselect from eject: the
  // eject path drops the fragment entirely.
  expect(page.url(), 'landed on the conversations list route').toMatch(/#\/conversations$/);
  expect(page.url(), 'the session id is gone from the URL').not.toContain(m.long_session_id);

  // ── SECOND Escape → leave the workspace to the DASHBOARD ─────────────────────
  await page.keyboard.press('Escape');

  await expect(page.locator('#main-content .panel-host').first()).toBeVisible();
  await expect(page.locator('.conv-rail')).toHaveCount(0);
  expect(page.url(), 'the dashboard drops the conversations fragment').not.toContain('#/conversations');
});
