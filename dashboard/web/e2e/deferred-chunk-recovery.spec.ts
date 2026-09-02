import { expect, test } from '@playwright/test';

test('a failed deferred feature keeps the shell usable and reloads into a coherent build', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /Recent Sessions/ })).toBeVisible();

  let abortedChunk = '';
  await page.route('**/static/assets/*.js', async (route) => {
    if (abortedChunk === '') {
      abortedChunk = route.request().url();
      await route.abort('failed');
      return;
    }
    await route.continue();
  });

  await page.getByRole('button', { name: 'Conversations' }).click();
  const recovery = page.getByRole('alert', { name: 'Conversations failed to load' });
  await expect(recovery).toBeVisible();
  expect(abortedChunk).toContain('/static/assets/');
  await expect(page.getByRole('heading', { name: 'cctally dashboard' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Dashboard', exact: true })).toBeVisible();

  await page.unroute('**/static/assets/*.js');
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'load' }),
    recovery.getByRole('button', { name: 'Reload dashboard' }).click(),
  ]);
  await page.getByRole('button', { name: 'Conversations' }).click();
  await expect(page.locator('.conv-rail')).toBeVisible();
});
