import { test, expect } from '@playwright/test';

async function openCardCorpus(page: import('@playwright/test').Page) {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: 'Run synthetic shell and patch fixtures' }).click();
  await expect(page.locator('.conv-reader-title')).toHaveText('Run synthetic shell and patch fixtures');
}

test('Codex shell and patch wires render as native cards without harness noise', async ({ page }) => {
  await openCardCorpus(page);

  const terminals = page.locator('.conv-term');
  // Five supported terminal wrappers. The two patch-over-exec wrappers route
  // to patch cards and the deliberately malformed future wrapper stays generic.
  await expect(terminals).toHaveCount(5);
  await expect(terminals.first().locator('.conv-chip-name')).toHaveText('exec');
  await expect(terminals.first()).toContainText("printf 'alpha\\n'");
  await expect(terminals.first()).toContainText('/synthetic/root-a/project-red');
  await expect(terminals.first()).toContainText('alpha');
  await expect(page.locator('.conv-term-badge--err')).toHaveCount(1);
  await expect(page.locator('.conv-term').filter({ hasText: 'seq 1 25' })).not.toHaveAttribute('open', '');

  const patch = page.locator('.conv-native-patch').filter({ hasText: 'synthetic-added.txt' });
  await expect(patch).toHaveCount(1);
  await expect(patch.locator('.conv-diff-row--add').first()).toContainText('alpha');
  await expect(patch.locator('.conv-diff-row--del').first()).toContainText('old');
  await expect(patch).toContainText('synthetic-old.txt → synthetic-new.txt');

  await expect(terminals.getByText('tools.exec_command', { exact: false })).toHaveCount(0);
  await expect(terminals.getByText('custom_tool_call_output', { exact: false })).toHaveCount(0);
  await expect(terminals.getByText('Script completed', { exact: false })).toHaveCount(0);
  await terminals.first().getByRole('button', { name: 'Load raw request payload' }).click();
  await expect(terminals.first().locator('.conv-native-raw pre')).toContainText('tools.exec_command');

  // The tool run is intentionally tall; navigate to the standalone final event
  // before asserting it so the virtualized reader mounts that retained item.
  await page.getByRole('button', { name: 'Jump to latest message' }).click();
  await expect(page.locator('.conv-native-patch').filter({ hasText: 'synthetic-summary.txt' })).toContainText('No diff retained');
  const diffLess = page.locator('.conv-native-patch').filter({ hasText: 'synthetic-summary.txt' });
  await expect(diffLess).toContainText('synthetic failure');
  await diffLess.getByRole('button', { name: 'Load raw event payload' }).click();
  await expect(diffLess.locator('.conv-native-raw pre')).toContainText('patch_apply_end');
});

test('Claude reference retains the canonical Bash and Edit card vocabulary', async ({ page }) => {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-row').filter({ hasText: 'Synthetic Claude terminal and edit reference' }).click();
  await expect(page.locator('.conv-term .conv-chip-name')).toHaveText('Bash');
  await expect(page.locator('.conv-term')).toContainText("printf 'alpha\\n'");
  await expect(page.locator('.conv-term')).toContainText('alpha');
  await expect(page.locator('.conv-diff-card > summary .conv-chip-name')).toHaveText('Edit');
  await expect(page.locator('.conv-diff-card')).toContainText('synthetic-edit.txt');
  await expect(page.locator('.conv-diff-row--del')).toContainText('old');
  await expect(page.locator('.conv-diff-row--add')).toContainText('new');
});

test.describe('compact Codex cards', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('terminal and patch cards stay inspectable without horizontal page overflow', async ({ page }) => {
    await openCardCorpus(page);
    await expect(page.locator('.conv-term').first()).toBeVisible();
    await expect(page.locator('.conv-native-patch').first()).toBeVisible();
    await expect.poll(() => page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    )).toBe(true);
    expect((await page.screenshot()).byteLength).toBeGreaterThan(1_000);
  });
});
