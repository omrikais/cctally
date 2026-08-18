import { expect, test, type Page } from '@playwright/test';

type HeaderGeometry = {
  actionsTop: number;
  height: number;
};

const BASKET_KEY = 'cctally:share:basket';

async function seedBasket(page: Page) {
  await page.addInitScript((key) => {
    localStorage.setItem(key, JSON.stringify([{
      id: 'e2e-header-basket',
      panel: 'weekly',
      template_id: 'weekly-recap',
      options: {
        format: 'md',
        theme: 'light',
        reveal_projects: false,
        no_branding: false,
        top_n: 5,
        period: { kind: 'current' },
        project_allowlist: null,
        show_chart: true,
        show_table: true,
      },
      added_at: '2026-05-12T09:00:00Z',
      data_digest_at_add: 'sha256:e2e-header',
      kernel_version: 1,
      label_hint: 'Weekly recap',
    }]));
  }, BASKET_KEY);
}

async function scrollToCondensedHeader(page: Page) {
  await page.goto('/');
  await page.locator('#main-content').hover();
  await expect(async () => {
    await page.mouse.wheel(0, 900);
    await expect(page.locator('.topbar.is-scrolled')).toBeVisible({ timeout: 500 });
  }).toPass({ timeout: 8000 });
}

async function injectDecoratedCodexAccountRow(page: Page) {
  await page.locator('header.topbar').evaluate((header) => {
    const condensed = header.querySelector('.topbar-condensed');
    if (condensed == null) throw new Error('condensed header is missing');
    const row = document.createElement('div');
    row.className = 'account-chip-row';
    row.setAttribute('role', 'radiogroup');
    row.setAttribute('aria-label', 'Codex account');
    for (const [label, active] of [
      ['All accounts', true],
      ['personal-account@codex', false],
    ] as const) {
      const chip = document.createElement('button');
      chip.className = `account-chip${active ? ' is-active' : ''}`;
      chip.textContent = label;
      row.append(chip);
    }
    header.insertBefore(row, condensed);
  });
}

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

test.describe('#587 — decorated Codex scrolled header stays within the viewport', () => {
  test.use({ hasTouch: true });

  for (const width of [375, 480, 640]) {
    test(`${width}px enforces the intended account-row visibility and page geometry`, async ({ page }) => {
      await page.setViewportSize({ width, height: 812 });
      await seedBasket(page);
      await scrollToCondensedHeader(page);
      await injectDecoratedCodexAccountRow(page);

      const geometry = await page.locator('header.topbar').evaluate((header) => {
        const row = header.querySelector<HTMLElement>('.account-chip-row');
        if (row == null) throw new Error('account row is missing');
        return {
          documentClientWidth: document.documentElement.clientWidth,
          documentScrollWidth: document.documentElement.scrollWidth,
          headerClientWidth: header.clientWidth,
          headerScrollWidth: header.scrollWidth,
          rowDisplay: getComputedStyle(row).display,
        };
      });

      expect(geometry.documentScrollWidth).toBeLessThanOrEqual(geometry.documentClientWidth);
      expect(geometry.headerScrollWidth).toBeLessThanOrEqual(geometry.headerClientWidth);
      expect(geometry.rowDisplay).toBe(width === 375 ? 'none' : 'flex');
    });
  }
});
