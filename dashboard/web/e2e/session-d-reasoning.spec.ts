import { test, expect, type Page } from '@playwright/test';

async function openByTitle(page: Page, source: 'Claude' | 'Codex', title: string) {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: source, exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: title }).click();
  await expect(page.locator('.conv-reader-body')).toBeVisible();
}

async function assertNoOverflow(page: Page) {
  await expect.poll(() => page.evaluate(
    () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
  )).toBe(true);
}

async function fillFind(page: Page, needle: string, count: number) {
  const input = page.locator('.conv-findbar input');
  if (!await input.isVisible()) await page.getByRole('button', { name: 'Find in conversation' }).click();
  await input.fill(needle);
  await expect(page.locator('.conv-findbar-count')).toContainText(`/ ${count}`);
  await input.press('Enter');
}

async function sessionDWireCounts(page: Page) {
  return page.evaluate(async () => {
    const match = location.hash.match(/#\/conversations\/source\/codex\/([^/]+)/);
    if (!match) throw new Error('qualified Codex route missing');
    const key = decodeURIComponent(match[1]);
    const detail = await fetch(`/api/conversation/${encodeURIComponent(key)}?limit=50`).then((response) => response.json());
    const blocks = detail.items.flatMap((item: { blocks?: Array<{ kind?: string; detail?: Record<string, unknown> }> }) => item.blocks ?? []);
    return {
      reasoning: blocks.filter((block: { kind?: string }) => block.kind === 'reasoning').length,
      lifecycleFallbacks: blocks.filter((block: { detail?: { lifecycle?: { event?: string } } }) =>
        block.detail?.lifecycle?.event === 'task_started' || block.detail?.lifecycle?.event === 'task_complete').length,
      markers: blocks.flatMap((block: { detail?: { markers?: unknown[] } }) => block.detail?.markers ?? []).length,
    };
  });
}

test('Session D renders native Codex reasoning, lifecycle fallbacks, and safe system actions', async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });

  await openByTitle(page, 'Claude', 'Synthetic Claude thinking reference');
  await expect(page.locator('.conv-chip--thinking')).toHaveCount(1);
  await expect(page.locator('.conv-chip--thinking .conv-chip-name')).toHaveText('Thinking');
  await expect(page.locator('.conv-codex-reasoning')).toHaveCount(0);

  await openByTitle(page, 'Codex', 'User-authored ::git-stage');
  expect(await sessionDWireCounts(page)).toEqual({ reasoning: 4, lifecycleFallbacks: 5, markers: 6 });
  await expect(page.locator('.conv-chip--thinking')).toHaveCount(0);
  await fillFind(page, 'Inspecting synthetic state', 2);
  await expect(page.locator('.conv-codex-reasoning').filter({ hasText: 'Inspecting synthetic state' }).first()).toBeVisible();
  expect((await page.locator('.conv-codex-reasoning').allTextContents()).join('\n')).not.toContain('**');

  await fillFind(page, 'Synthetic provider summary.', 1);
  const summaryBody = page.locator('.conv-codex-reasoning--expandable').filter({ hasText: 'Synthetic provider summary.' });
  await summaryBody.locator('summary').click();
  await expect(summaryBody.locator('.conv-codex-reasoning-summary')).toContainText('Synthetic provider summary.');
  await expect(summaryBody.locator('.conv-codex-reasoning-body')).toContainText('Detailed synthetic reasoning body.');

  await fillFind(page, 'Errored lifecycle answer.', 1);
  const errorLifecycle = page.locator('.conv-meta--notification').filter({ hasText: 'Errored lifecycle answer.' });
  await expect(errorLifecycle).toContainText('Codex task complete');
  await expect(errorLifecycle).not.toContainText('Background task');
  await errorLifecycle.locator('summary').click();
  await expect(errorLifecycle.locator('.conv-codex-lifecycle')).toContainText('Synthetic lifecycle failure');
  await expect(errorLifecycle.getByRole('button', { name: 'Load raw event payload' })).toBeVisible();

  await fillFind(page, 'Synthetic closeout prose remains visible.', 1);
  const markerItem = page.locator('.conv-item--assistant').filter({ hasText: 'Synthetic closeout prose remains visible.' });
  await expect(markerItem.locator('.conv-system-action')).toHaveCount(6);
  await expect(markerItem.locator('.conv-system-actions')).toContainText('Changes staged');
  await expect(markerItem.locator('.conv-system-actions')).toContainText('Pull request created');
  await expect(markerItem.locator('.conv-system-actions')).toContainText('Memory references attached');
  await expect(markerItem).not.toContainText('::git-create-branch');
  await expect(markerItem).not.toContainText('<oai-mem-citation>');
  await expect(markerItem).not.toContainText('/synthetic/project');
  await expect(markerItem.getByRole('button', { name: 'Load raw event payload' })).toBeVisible();

  await fillFind(page, '::git-unknown', 1);
  await expect(page.locator('.conv-reader-body')).toContainText('::git-unknown');
  await fillFind(page, '::git-stage{cwd="/synthetic/malformed"', 1);
  await expect(page.locator('.conv-reader-body')).toContainText('::git-stage{cwd="/synthetic/malformed"');
  await fillFind(page, '<oai-mem-citation>', 1);
  await expect(page.locator('.conv-reader-body')).toContainText('<oai-mem-citation>');
  await assertNoOverflow(page);
  expect(consoleErrors).toEqual([]);
});

test.describe('Session D compact viewport', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('keeps reasoning and system actions readable without overflow', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });
    await openByTitle(page, 'Codex', 'User-authored ::git-stage');
    expect(await sessionDWireCounts(page)).toEqual({ reasoning: 4, lifecycleFallbacks: 5, markers: 6 });
    await expect(page.locator('.conv-chip--thinking')).toHaveCount(0);
    await fillFind(page, 'Synthetic closeout prose remains visible.', 1);
    await expect(page.locator('.conv-system-actions')).toContainText('Memory references attached');
    await expect(page.locator('.conv-meta--notification').first()).toContainText('Codex task');
    await assertNoOverflow(page);
    expect((await page.screenshot()).byteLength).toBeGreaterThan(1_000);
    expect(consoleErrors).toEqual([]);
  });
});
