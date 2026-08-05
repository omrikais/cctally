import { test, expect } from '@playwright/test';
import type { Page, Request } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  appendOccurrenceFindTurn,
  loadManifest,
  openConversation,
  removeOccurrenceFindReasoning,
  settleScroller,
  READER_BODY,
} from './utils';

// Scenario 5 (spec §4.5) — find-bar focus trap + Esc containment. Tab from the
// find input cycles within the bar controls; Esc while a bar BUTTON holds focus
// closes only the find bar — the reader stays mounted (view stays conversations,
// URL intact) and focus restores to the thread. RED lever: move Esc handling from
// the bar container back onto the input (the #217 S4 teardown bug) so Esc on a
// button bubbles to the document and tears the reader down.
const m = loadManifest();
const FINDBAR = '.conv-findbar';
const HERE = dirname(fileURLToPath(import.meta.url));
const EVIDENCE = resolve(HERE, '.evidence');

function captureBrowserErrors(page: Page) {
  const errors: string[] = [];
  const failedRequests: string[] = [];
  page.on('console', (message: { type: () => string; text: () => string }) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error: Error) => errors.push(error.message));
  page.on('requestfailed', (request: Request) => {
    failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText ?? 'failed'}`);
  });
  return { errors, failedRequests };
}

test('occurrence-exact find counts and lands every rendered match', async ({ page }) => {
  test.setTimeout(120_000);
  mkdirSync(EVIDENCE, { recursive: true });
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: m.occurrence_find_title }).click();
  await expect(page.locator('.conv-reader-item').first()).toContainText(m.occurrence_find_needle);
  const browser = captureBrowserErrors(page);

  await page.getByRole('button', { name: 'Find in conversation' }).click();
  await page.locator('.conv-findbar-input').fill(m.occurrence_find_needle);
  await expect(page.locator('.conv-findbar-count')).toBeVisible();

  // The canonical visible projection contains 12 cross-surface matches plus a
  // 205-occurrence tail, forcing navigation across 100/200 page boundaries.
  // Before #482 the UI reports containing items instead, so this is the
  // mechanism-level RED and the screenshot preserves the product failure.
  await expect(page.locator('.conv-findbar-count')).toHaveText(`1 / ${m.occurrence_find_total} matches`);
  await page.screenshot({ path: resolve(EVIDENCE, '482-final-desktop.png'), fullPage: true });

  const assertCurrent = async (index: number, previousId?: string) => {
    await expect(page.locator('.conv-findbar-count')).toHaveText(`${index} / ${m.occurrence_find_total} matches`);
    const current = page.locator('mark[data-find-current="true"]');
    await expect(current.first()).toBeVisible();
    if (previousId) {
      await expect.poll(async () => current.first().getAttribute('data-find-occurrence-id'))
        .not.toBe(previousId);
    }
    const ids = await current.evaluateAll((marks) => [...new Set(marks.map(
      (mark) => mark.getAttribute('data-find-occurrence-id'),
    ))]);
    expect(ids).toHaveLength(1);
    return ids[0]!;
  };
  let currentId = await assertCurrent(1);
  const forwardIds = new Set<string>([currentId]);
  const next = page.getByRole('button', { name: 'Next match' });
  for (let i = 2; i <= m.occurrence_find_total; i++) {
    await next.click();
    currentId = await assertCurrent(i, currentId);
    forwardIds.add(currentId);
  }
  expect(forwardIds.size).toBe(m.occurrence_find_total);
  await next.click();
  currentId = await assertCurrent(1, currentId);

  const previous = page.getByRole('button', { name: 'Previous match' });
  const reverseIds = new Set<string>();
  for (let i = m.occurrence_find_total; i >= 1; i--) {
    await previous.click();
    currentId = await assertCurrent(i, currentId);
    reverseIds.add(currentId);
  }
  expect(reverseIds).toEqual(forwardIds);
  await previous.click();
  await assertCurrent(m.occurrence_find_total, currentId);
  expect(await page.evaluate(
    () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
  )).toBe(true);
  expect(browser.errors).toEqual([]);
  expect(browser.failedRequests).toEqual([]);
});

test('coordinates one formatting-spanning occurrence across three render leaves', async ({ page }) => {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: m.occurrence_find_title }).click();
  await expect(page.locator('.conv-reader-item').first()).toContainText(m.occurrence_find_needle);
  await page.getByRole('button', { name: 'Find in conversation' }).click();
  await page.locator('.conv-findbar-input').fill(m.occurrence_find_cross_leaf_query);
  await expect(page.locator('.conv-findbar-count')).toHaveText('1 / 1 matches');
  const marks = page.locator('mark[data-find-current="true"]');
  await expect(marks).toHaveCount(3);
  expect(await marks.allTextContents()).toEqual(['a', m.occurrence_find_needle, 'c']);
  expect(new Set(await marks.evaluateAll((nodes) => nodes.map(
    (node) => node.getAttribute('data-find-occurrence-id'),
  ))).size).toBe(1);
});

test('retains regex and case-sensitive occurrence semantics', async ({ page }) => {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: m.occurrence_find_title }).click();
  await expect(page.locator('.conv-reader-item').first()).toContainText(m.occurrence_find_needle);
  await page.getByRole('button', { name: 'Find in conversation' }).click();
  const input = page.locator('.conv-findbar-input');
  const count = page.locator('.conv-findbar-count');
  await input.fill(m.occurrence_find_case_query);
  await expect(count).toHaveText('1 / 3 matches');
  await page.getByRole('button', { name: 'Case-sensitive' }).click();
  await expect(count).toHaveText('1 / 1 matches');
  await input.fill(m.occurrence_find_regex_query);
  await page.getByRole('button', { name: 'Regular expression' }).click();
  await expect(count).toHaveText('1 / 2 matches');
  await page.getByRole('button', { name: 'Case-sensitive' }).click();
  await expect(count).toHaveText('1 / 4 matches');
});

test.describe('compact occurrence find', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('shows the exact mobile count without horizontal overflow', async ({ page }) => {
    mkdirSync(EVIDENCE, { recursive: true });
    await page.goto('/#/conversations');
    await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
    await page.locator('.conv-rail-row').filter({ hasText: m.occurrence_find_title }).click();
    await expect(page.locator('.conv-reader-item').first()).toContainText(m.occurrence_find_needle);
    const browser = captureBrowserErrors(page);
    await page.getByRole('button', { name: 'Find in conversation' }).click();
    await page.locator('.conv-findbar-input').fill(m.occurrence_find_needle);
    await expect(page.locator('.conv-findbar-count')).toBeVisible();
    await expect(page.locator('.conv-findbar-count')).toHaveText(`1 / ${m.occurrence_find_total} matches`);
    await page.screenshot({ path: resolve(EVIDENCE, '482-final-mobile.png'), fullPage: true });
    expect(await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    )).toBe(true);
    expect(browser.errors).toEqual([]);
    expect(browser.failedRequests).toEqual([]);
  });
});

test('opens only the exact disclosure and reconciles append and removal', async ({ page }) => {
  test.setTimeout(45_000);
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: m.occurrence_find_title }).click();
  await expect(page.locator('.conv-reader-item').first()).toContainText(m.occurrence_find_needle);
  const reasoning = page.locator('details.conv-codex-reasoning');
  await expect(reasoning).not.toHaveAttribute('open', '');
  const reasoningKey = await reasoning.getAttribute('data-disclosure-key');
  const initiallyClosed = new Set(await page.locator('details[data-disclosure-key]:not([open])')
    .evaluateAll((details) => details.map((detail) => detail.getAttribute('data-disclosure-key'))));
  let findRequests = 0;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.endsWith('/find')) findRequests += 1;
  });
  await page.getByRole('button', { name: 'Find in conversation' }).click();
  await page.locator('.conv-findbar-input').fill(m.occurrence_find_reasoning_query);
  await expect(page.locator('.conv-findbar-count')).toHaveText('1 / 1 matches');
  const selectedId = await page.locator('mark[data-find-current="true"]').first()
    .getAttribute('data-find-occurrence-id');
  await expect(reasoning).toHaveAttribute('open', '');
  const newlyOpen = (await page.locator('details[data-disclosure-key][open]')
    .evaluateAll((details) => details.map((detail) => detail.getAttribute('data-disclosure-key'))))
    .filter((key) => initiallyClosed.has(key));
  expect(newlyOpen).toEqual([reasoningKey]);

  const beforeAppend = findRequests;
  appendOccurrenceFindTurn();
  await expect.poll(() => findRequests, { timeout: 12_000 }).toBeGreaterThan(beforeAppend);
  await expect(page.locator('.conv-findbar-count')).toHaveText('1 / 1 matches');
  expect(await page.locator('mark[data-find-current="true"]').first()
    .getAttribute('data-find-occurrence-id')).toBe(selectedId);

  const beforeRemoval = findRequests;
  removeOccurrenceFindReasoning(m);
  await expect.poll(() => findRequests, { timeout: 12_000 }).toBeGreaterThan(beforeRemoval);
  await expect(page.locator('.conv-findbar-count')).toContainText('0 / 0 matches');
  await expect(page.locator('.conv-findbar-count')).toContainText('previous match changed');
});

test('the find bar traps Tab focus and Esc on a button closes only the bar', async ({ page }) => {
  await openConversation(page, m.long_session_id);
  await expect(page.locator(READER_BODY)).toBeVisible();
  await settleScroller(page);
  const url0 = page.url();

  // Open the find bar; the input takes focus.
  await page.locator(READER_BODY).click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('/');
  await expect(page.locator(FINDBAR)).toBeVisible();
  await expect(page.locator('.conv-findbar-input')).toBeFocused();
  // A match enables the prev/next nav buttons (more controls in the trap).
  await page.locator('.conv-findbar-input').fill(m.jump_target_needle);
  await expect(page.locator('.conv-findbar-count')).toContainText('1 / 1');

  // Tab through more than one full cycle: focus never leaves the bar.
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press('Tab');
    const withinBar = await page.evaluate(() => {
      const a = document.activeElement;
      const bar = document.querySelector('.conv-findbar');
      return !!(a && bar && bar.contains(a));
    });
    expect(withinBar, `Tab #${i + 1} kept focus inside the find bar`).toBe(true);
  }

  // Move focus onto a bar BUTTON, then press Escape.
  await page.locator('.conv-findbar-close').focus();
  await expect(page.locator('.conv-findbar-close')).toBeFocused();
  await page.keyboard.press('Escape');

  // The find bar closed, but the reader is intact (Esc did NOT tear it down).
  await expect(page.locator(FINDBAR)).toHaveCount(0);
  await expect(page.locator(READER_BODY)).toBeVisible();
  expect(page.url(), 'still on the same conversation route').toBe(url0);
  // Focus restored to the thread (not left on a detached button / the body).
  const focusOk = await page.evaluate(() => {
    const a = document.activeElement;
    return !!a && a !== document.body && document.querySelector('.conv-reader') !== null
      && (document.querySelector('.conv-reader') as HTMLElement).contains(a);
  });
  expect(focusOk, 'focus restored inside the reader').toBe(true);
});
