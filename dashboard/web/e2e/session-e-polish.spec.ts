import { test, expect, type Page } from '@playwright/test';

async function openSessionD(page: Page) {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole(
    'button', { name: 'Codex', exact: true },
  ).click();
  await page.locator('.conv-rail-row').filter({
    hasText: 'User-authored ::git-stage',
  }).click();
  await expect(page.locator('.conv-reader-body')).toBeVisible();
}

test.describe('Session E compact reader polish', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('keeps provider labels intact and the complete Codex token strip unclipped', async ({ page }) => {
    await openSessionD(page);
    const lifecycle = page.locator('.conv-meta--notification').filter({
      hasText: 'Errored lifecycle answer.',
    });
    await expect(lifecycle).toHaveCount(1);
    const label = lifecycle.locator('.conv-meta-label');
    await expect(label).toHaveAttribute('title', 'Codex task complete');
    const labelMetrics = await label.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        height: element.getBoundingClientRect().height,
        lineHeight: Number.parseFloat(style.lineHeight),
        overflowWrap: style.overflowWrap,
        whiteSpace: style.whiteSpace,
      };
    });
    expect(labelMetrics.whiteSpace).toBe('nowrap');
    expect(labelMetrics.overflowWrap).toBe('normal');
    expect(labelMetrics.height).toBeLessThanOrEqual(labelMetrics.lineHeight + 1);

    const stripMetrics = await page.locator('.conv-provider-strip').evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
    }));
    expect(stripMetrics.clientHeight).toBeGreaterThanOrEqual(stripMetrics.scrollHeight);
    const tokenStrip = page.locator('.conv-provider-tokens');
    await expect(tokenStrip).toContainText('in 0');
    await expect(tokenStrip).toContainText('out 0');
    await expect(tokenStrip).toContainText('cached in 0');
    await expect(tokenStrip).toContainText('reasoning out 0');
    expect(await tokenStrip.evaluate((element) => getComputedStyle(element).whiteSpace)).toBe('normal');
    expect(await page.evaluate(
      () => document.documentElement.scrollWidth === document.documentElement.clientWidth,
    )).toBe(true);
  });

  test('keeps retained native families silent while long provider token fields wrap', async ({ page }) => {
    await page.goto('/#/conversations');
    await page.locator('.conv-rail-source').getByRole(
      'button', { name: 'Codex', exact: true },
    ).click();
    const sessionE = page.locator('.conv-rail-row').filter({
      hasText: 'Session E visible prompt A',
    });
    await expect(sessionE).toHaveCount(1);
    await sessionE.click();

    const tokenStrip = page.locator('.conv-provider-tokens');
    await expect(tokenStrip).toContainText('in 123.5M');
    await expect(tokenStrip).toContainText('out 76.5M');
    await expect(tokenStrip).toContainText('cached in 98.8M');
    await expect(tokenStrip).toContainText('reasoning out 54.3M');
    const strip = page.locator('.conv-provider-strip');
    expect(await strip.evaluate((element) => element.clientHeight >= element.scrollHeight)).toBe(true);

    await expect(page.locator('.conv-reader-item[data-item-index]')).toHaveCount(4);
    const reader = page.locator('.conv-reader-body');
    await expect(reader).not.toContainText('SESSION_E_PRIVATE_INSTRUCTION_CANARY');
    await expect(reader).not.toContainText('/synthetic/private/session-e/workspace');
    await expect(reader).not.toContainText('native-secret-opaque-335');
    expect(await page.evaluate(
      () => document.documentElement.scrollWidth === document.documentElement.clientWidth,
    )).toBe(true);
  });
});
