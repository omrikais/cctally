import { expect, test } from '@playwright/test';

for (const { reason, notice, retry } of [
  { reason: 'maintenance', notice: 'busy with maintenance', retry: true },
  { reason: 'schema_behind', notice: 'behind this version of cctally', retry: false },
]) {
  test(`deep-linked reader explains ${reason} without inventing a missing conversation`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/api/conversation/*', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (/^\/api\/conversation\/[^/]+$/.test(path)) {
        await route.fulfill({ json: { status: 'degraded', degraded_reason: reason, items: [], page: {} } });
      } else {
        await route.continue();
      }
    });
    await page.goto('/#/conversations');
    await expect(page.locator('.conv-rail-row').first()).toBeVisible();
    await page.locator('.conv-rail-row').first().click();
    await expect(page).toHaveURL(/#\/conversations\/source\//);
    await page.reload();
    await expect(page.locator('.conv-reader')).toContainText(notice);
    await expect(page.locator('.conv-reader')).not.toContainText("Couldn't load the conversation.");
    await expect(page.locator('.conv-reader').getByRole('button', { name: 'Retry' })).toHaveCount(retry ? 1 : 0);
    expect(errors).toEqual([]);
    expect((await page.screenshot()).byteLength).toBeGreaterThan(1_000);
  });
}

for (const { body, status, notice } of [
  { body: { status: 'degraded', degraded_reason: 'maintenance' }, status: 200, notice: 'busy with maintenance' },
  { body: { error: 'broken' }, status: 500, notice: "Couldn't load the outline." },
]) {
  test(`the open outline names its read state (${status}) instead of loading forever`, async ({ page }) => {
    await page.route('**/api/conversation/*/outline?*', (route) => route.fulfill({ status, json: body }));
    await page.goto('/#/conversations');
    await page.locator('.conv-rail-row').first().click();
    if (await page.getByRole('navigation', { name: 'Session outline' }).count() === 0) {
      await page.getByRole('button', { name: 'Toggle session outline' }).first().click();
    }
    await expect(page.getByRole('navigation', { name: 'Session outline' })).toContainText(notice);
    await expect(page.getByRole('navigation', { name: 'Session outline' })).not.toContainText('Loading outline…');
  });
}

test('search page two explains maintenance instead of reporting a failed search', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let pages = 0;
  await page.route('**/api/conversation/search?*', async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('offset') !== '0') {
      pages += 1;
      await route.fulfill({ json: { status: 'degraded', degraded_reason: 'maintenance', results: [], total: 0 } });
      return;
    }
    const response = await route.fetch();
    const body = await response.json();
    if (!Array.isArray(body.hits) || body.hits.length === 0) {
      throw new Error('The browser fixture must provide a nonempty first search page');
    }
    await route.fulfill({ json: { ...body, total: body.hits.length + 1 } });
  });
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-search-input').fill('seed');
  await expect(page.locator('.conv-rail-more')).toBeVisible();
  await page.locator('.conv-rail-more').click();
  await expect(page.locator('.conv-rail-list')).toContainText('busy with maintenance');
  await expect(page.locator('.conv-rail-list .conv-rail-row--hit').first()).toBeVisible();
  await expect(page.locator('.conv-rail-list')).not.toContainText('Search failed.');
  await expect(page.locator('.conv-rail-more')).toHaveCount(0);
  await expect(page.locator('.conv-rail-list .conv-rail-row--hit')).not.toHaveCount(0);
  expect(pages).toBe(1);
  expect(errors).toEqual([]);
  expect((await page.screenshot()).byteLength).toBeGreaterThan(1_000);
});
