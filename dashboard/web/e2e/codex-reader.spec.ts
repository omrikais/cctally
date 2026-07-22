import { test, expect } from '@playwright/test';
import { appendFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const MODERN_ROLLOUT = resolve(HERE, '.runtime/scratch/codex-main/sessions/2026/07/20/modern-full.jsonl');

test('qualified Codex browse and shared reader preserve native meaning', async ({ page }) => {
  await page.goto('/#/conversations');

  const sourceSwitch = page.locator('.conv-rail-source');
  await sourceSwitch.getByRole('button', { name: 'Codex', exact: true }).click();
  await expect(page.locator('.conv-rail-row')).toHaveCount(8);

  const modern = page.locator('.conv-rail-row').filter({ hasText: 'Synthetic first meaningful user prompt' });
  await modern.click();

  await expect(page.locator('.conv-provider-strip')).toContainText('cached in 300');
  await expect(page.locator('.conv-provider-strip')).toContainText('reasoning out 100');
  await expect(page.locator('.conv-provider-strip')).toContainText('media unavailable');
  await expect(page.locator('.conv-reader-item[data-item-index]')).toHaveCount(8);
  await expect(page.locator('.conv-codex-reasoning').filter({ hasText: 'Synthetic agent reasoning' })).toBeVisible();
  await expect(page.locator('.conv-native-patch').filter({ hasText: 'synthetic.txt' })).toBeVisible();

  const outlineToggle = page.getByRole('button', { name: 'Toggle session outline' });
  if (await outlineToggle.getAttribute('aria-pressed') === 'false') await outlineToggle.click();
  await page.getByRole('tab', { name: 'Files', exact: true }).click();
  await expect(page.locator('.conv-outline-files')).toContainText('synthetic.txt');
  await expect(page.locator('.conv-outline-files')).toContainText('apply_patch');

  const tool = page.locator('.conv-chip--tool').filter({ hasText: 'fixture_function' });
  await tool.locator('summary').click();
  await expect(tool.getByRole('button', { name: 'load full result' })).toBeVisible();
  await tool.getByRole('button', { name: 'load full result' }).click();
  await expect(tool.getByRole('button', { name: 'load full result' })).toHaveCount(0);

  await page.getByRole('button', { name: 'Find in conversation' }).click();
  await page.locator('.conv-findbar input').fill('Synthetic');
  await expect(page.locator('.conv-findbar-count')).toHaveText('1 / 5');
});

test('qualified Codex parent and child links remain opaque and navigable', async ({ page }) => {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();

  await page.locator('.conv-rail-row').filter({ hasText: 'Parent thread question' }).click();
  const child = page.getByRole('button', { name: /Child → Child thread question/ });
  await expect(child).toBeVisible();
  await child.click();

  await expect(page.locator('.conv-reader-title')).toHaveText('Child thread question');
  await expect(page.getByRole('button', { name: /Parent · Parent thread question/ })).toBeVisible();
  await expect(page).toHaveURL(/#\/conversations\/source\/codex\/v1\./);
});

test('qualified Codex live-tail appends without duplicating the retained window', async ({ page }) => {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: 'Synthetic first meaningful user prompt' }).click();
  await expect(page.locator('.conv-reader-item[data-item-index]')).toHaveCount(8);

  appendFileSync(MODERN_ROLLOUT, `${JSON.stringify({
    payload: {
      images: [], local_images: [], message: 'Synthetic live-tail event',
      text_elements: [{ text: 'Synthetic live-tail event' }], type: 'user_message',
    },
    timestamp: '2026-07-14T12:14:00Z', type: 'event_msg',
  })}\n`);

  await expect(page.locator('.conv-reader-body p').filter({ hasText: 'Synthetic live-tail event' })).toBeVisible({ timeout: 12_000 });
  await expect(page.locator('.conv-reader-item[data-item-index]')).toHaveCount(9);
});

test('All composes qualified sources locally and collision state stays isolated', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (text: string) => localStorage.setItem('task-c.clipboard', text) },
    });
  });
  const collectionRequests: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/api/conversations?')) collectionRequests.push(request.url());
  });
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'All', exact: true }).click();

  await expect(page.locator('.conv-rail-row')).toHaveCount(16);
  await expect(page.locator('.conv-rail-row').filter({ hasText: 'Root A red prompt' })).toBeVisible();
  await expect(page.locator('.conv-rail-row').filter({ hasText: 'Root B blue prompt' })).toBeVisible();
  await expect(page.locator('.conv-rail-row').filter({ hasText: 'Claude seed user prompt distinct from codex' })).toBeVisible();
  expect(collectionRequests.some((url) => new URL(url).searchParams.get('source') === 'all')).toBe(false);
  expect(collectionRequests.some((url) => new URL(url).searchParams.get('source') === 'claude')).toBe(true);
  expect(collectionRequests.some((url) => new URL(url).searchParams.get('source') === 'codex')).toBe(true);

  await page.locator('.conv-rail-row').filter({ hasText: 'Root A red prompt' }).click();
  await expect(page.locator('.conv-reader-body')).toContainText('Root A red response');
  await page.locator('.conv-item--assistant').getByRole('button', { name: 'Bookmark this turn' }).click();
  await expect(page.getByRole('button', { name: 'Remove bookmark' })).toHaveCount(1);

  await page.locator('.conv-rail-row').filter({ hasText: 'Root B blue prompt' }).click();
  await expect(page.locator('.conv-reader-body')).toContainText('Root B blue response');
  await expect(page.getByRole('button', { name: 'Remove bookmark' })).toHaveCount(0);

  await page.locator('.conv-rail-row').filter({ hasText: 'Claude seed user prompt distinct from codex' }).click();
  await expect(page.locator('.conv-reader-body')).toContainText('Claude seed assistant reply distinct from codex');
  await page.locator('.conv-item--assistant').getByRole('button', { name: 'Copy link to this turn' }).click();
  const copied = await page.evaluate(() => localStorage.getItem('task-c.clipboard'));
  expect(copied).toMatch(/#\/conversations\/source\/claude\/v1\..+\/cliv1_/);
  await page.goto(copied!);
  await expect(page.locator('.conv-reader-body')).toContainText('Claude seed assistant reply distinct from codex');

  const restore = await page.evaluate(async () => {
    const match = location.hash.match(/#\/conversations\/source\/claude\/([^/]+)/);
    if (!match) throw new Error('qualified Claude route missing');
    const key = decodeURIComponent(match[1]);
    const detail = await fetch(`/api/conversation/${encodeURIComponent(key)}`).then((response) => response.json());
    const uuid = detail.items[1].item_key as string;
    const identity = JSON.stringify(['claude', key]);
    localStorage.setItem('cctally.conv.readingPos', JSON.stringify({ [identity]: { uuid, ts: Date.now() } }));
    return { key, uuid };
  });
  await page.goto(`/#/conversations/source/claude/${encodeURIComponent(restore.key)}`);
  await expect(page.locator('.conv-reader-body')).toContainText('Claude seed assistant reply distinct from codex');
  const restoredStoredUuid = await page.evaluate(({ key }) => {
    const map = JSON.parse(localStorage.getItem('cctally.conv.readingPos') ?? '{}');
    return map[JSON.stringify(['claude', key])]?.uuid ?? null;
  }, restore);
  expect(restoredStoredUuid).toBe(restore.uuid);
});

test('mixed comparison and export label sources without coercing provider metrics', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (text: string) => localStorage.setItem('task-c.clipboard', text) },
    });
  });
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'All', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: 'Claude seed user prompt distinct from codex' }).click();
  await expect(page.locator('.conv-reader-body')).toContainText('Claude seed assistant reply distinct from codex');
  const compareButton = page.locator('.conv-compare-with');
  if (await compareButton.isVisible()) {
    await compareButton.click();
  } else {
    await page.locator('.conv-overflow-toggle').click();
    await page.getByRole('menuitem', { name: /Compare with/ }).click();
  }

  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: 'Root A red prompt' }).click();
  await expect(page.locator('.conv-cmp-head')).toContainText('Claude');
  await expect(page.locator('.conv-cmp-head')).toContainText('Codex');
  await expect(page.locator('[data-metric="tokens"]')).toContainText('provider-specific');
  await expect(page.locator('[data-metric="duration"]')).toContainText('unavailable');
  await expect(page.locator('[data-metric="files"]')).toContainText('provider-specific');

  await page.getByRole('button', { name: 'Copy source-labelled comparison export' }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('task-c.clipboard'))).toContain('Run A · Claude');
  const exported = await page.evaluate(() => localStorage.getItem('task-c.clipboard'));
  expect(exported).toContain('Run B · Codex');
  expect(exported).toContain('Claude seed user prompt distinct from codex');
  expect(exported).toContain('Root A red prompt');

  await page.getByRole('button', { name: 'Swap the two sessions' }).click();
  await expect(page.locator('.conv-cmp-head-side').first()).toContainText('Codex');
  await page.getByRole('button', { name: 'Close comparison' }).click();
  await expect(page.locator('.conv-cmp')).toHaveCount(0);
});

test.describe('compact mixed-source reader', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('All and mixed comparison stay usable at a compact viewport', async ({ page }) => {
    await page.goto('/#/conversations');
    await page.locator('.conv-rail-source').getByRole('button', { name: 'All', exact: true }).click();
    await expect(page.locator('.conv-rail-row').filter({ hasText: 'Root A red prompt' })).toBeVisible();
    await expect(page.locator('.conv-rail-row').filter({ hasText: 'Root B blue prompt' })).toBeVisible();

    await page.locator('.conv-rail-row').filter({ hasText: 'Claude seed user prompt distinct from codex' }).click();
    await expect(page.locator('.conv-reader-body')).toContainText('Claude seed assistant reply distinct from codex');
    await page.locator('.conv-overflow-toggle').click();
    await page.getByRole('menuitem', { name: /Compare with/ }).click();

    await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
    await page.locator('.conv-rail-row').filter({ hasText: 'Root A red prompt' }).click();
    await expect(page.locator('.conv-cmp-head')).toContainText('Claude');
    await expect(page.locator('.conv-cmp-head')).toContainText('Codex');
    await expect(page.locator('[data-metric="tokens"]')).toContainText('provider-specific');
    await expect(page.locator('[data-metric="duration"]')).toContainText('unavailable');
    await expect(page.locator('[data-metric="files"]')).toContainText('provider-specific');
    await expect.poll(() => page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    )).toBe(true);
    expect((await page.screenshot()).byteLength).toBeGreaterThan(1_000);
  });
});
