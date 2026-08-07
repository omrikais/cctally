import { expect, test, type Page } from '@playwright/test';

type HeaderGeometry = {
  actionsTop: number;
  height: number;
};

async function headerGeometry(page: Page): Promise<HeaderGeometry> {
  return page.locator('header.topbar').evaluate((header) => {
    const actions = header.querySelector<HTMLElement>('.topbar-actions');
    if (actions == null) throw new Error('topbar actions are missing');
    return {
      actionsTop: actions.getBoundingClientRect().top,
      height: header.getBoundingClientRect().height,
    };
  });
}

test('desktop status age changes do not move the header actions onto a second row', async ({ page }) => {
  await page.setViewportSize({ width: 1794, height: 856 });
  await page.goto('/');

  await page.locator('header.topbar').evaluate((header) => {
    const actions = header.querySelector('.topbar-actions');
    if (actions == null) throw new Error('topbar actions are missing');
    const brand = header.querySelector('.topbar-brand');
    if (brand == null) throw new Error('topbar brand is missing');
    const version = document.createElement('span');
    version.className = 'brand-version';
    version.textContent = 'v1.92.3';
    brand.append(version);
    if (actions.querySelector('.source-status-chip') == null) {
      const sourceStatus = document.createElement('span');
      sourceStatus.className = 'source-status-chip';
      sourceStatus.textContent = 'fresh';
      actions.prepend(sourceStatus);
    }
    const row = document.createElement('div');
    row.className = 'account-chip-row';
    row.setAttribute('role', 'radiogroup');
    for (const [label, hint, className] of [
      ['All accounts', '', 'account-chip is-active'],
      ['omrikais@me.com (pro)', '60%', 'account-chip'],
      ['omrikais@me.com (team)', '', 'account-chip'],
      ['omrikais@gmail.com', '', 'account-chip'],
      ['Unattributed', '', 'account-chip is-dimmed'],
    ]) {
      const chip = document.createElement('button');
      chip.className = className;
      chip.textContent = label;
      if (hint) {
        const hintNode = document.createElement('span');
        hintNode.className = 'account-chip-hint';
        hintNode.textContent = hint;
        chip.append(hintNode);
      }
      row.append(chip);
    }
    header.insertBefore(row, actions);
  });

  const sync = page.locator('#sync-chip');
  await sync.evaluate((node) => { node.textContent = 'synced 1m ago'; });
  const shortAge = await headerGeometry(page);

  await sync.evaluate((node) => { node.textContent = 'synced 59s ago'; });
  const longAge = await headerGeometry(page);

  expect(shortAge.height).toBeLessThanOrEqual(56);
  expect(longAge.height).toBe(shortAge.height);
  expect(longAge.actionsTop).toBe(shortAge.actionsTop);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1794);
});
