import { expect, test, type Page } from '@playwright/test';

const MATRIX = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
] as const;

async function seedBusyRefusal(page: Page) {
  await page.route('**/api/sync', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'ok', warnings: [{ code: 'sync_busy' }] }),
  }));
}

for (const viewport of MATRIX) {
  test(`sync_busy is visible and informational on ${viewport.name}`, async ({ page }) => {
    const browserErrors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') browserErrors.push(message.text());
    });
    page.on('pageerror', (error) => browserErrors.push(error.message));

    await page.setViewportSize(viewport);
    await page.goto('/');
    await seedBusyRefusal(page);
    await page.locator('.topbar-sync').click();

    const chip = page.locator('#sync-chip');
    await expect(chip).toHaveText('refresh busy');
    await expect(chip).toHaveClass(/sync-busy-notice/);
    await expect(chip).not.toHaveClass(/sync-error/);
    await expect(chip).not.toHaveClass(/sync-chip--stale/);
    await expect(chip).toHaveAttribute('aria-live', 'polite');
    await expect(chip).toHaveCSS('color', 'rgb(129, 140, 248)');
    await expect(page.locator('.topbar-sync .icon'))
      .toHaveCSS('color', 'rgb(129, 140, 248)');
    expect(browserErrors).toEqual([]);
  });
}
