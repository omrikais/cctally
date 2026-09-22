import { expect, test, type Page } from '@playwright/test';

const MATRIX = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
] as const;

async function selectSource(page: Page, source: 'claude' | 'codex' | 'all') {
  const segment = page.locator(`.source-seg[data-source="${source}"]`);
  await segment.click();
  await expect(segment).toHaveClass(/is-active/);
}

for (const viewport of MATRIX) {
  test(`Current Week preserves provider ownership at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    const browserErrors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') browserErrors.push(message.text());
    });
    page.on('pageerror', (error) => browserErrors.push(error.message));

    await page.setViewportSize(viewport);
    await page.goto('/');

    for (const [source, title] of [
      ['claude', 'Current Cycle — per-percent milestones'],
      ['codex', 'Current Cycle — per-percent milestones'],
      ['all', 'Current Usage — provider cycles'],
    ] as const) {
      await selectSource(page, source);
      const hero = page.locator('[data-hero-strip]');
      await hero.focus();
      await page.keyboard.press('Enter');

      const dialog = page.getByRole('dialog', { name: title });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole('heading', { name: title })).toBeFocused();

      const duplicateIds = await dialog.locator('[id]').evaluateAll((nodes) => {
        const ids = nodes.map((node) => node.id);
        return ids.filter((id, index) => ids.indexOf(id) !== index);
      });
      expect(duplicateIds).toEqual([]);

      const geometry = await dialog.evaluate((node) => {
        const rect = node.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          viewport: window.innerWidth,
          documentWidth: document.documentElement.scrollWidth,
        };
      });
      expect(geometry.left).toBeGreaterThanOrEqual(-1);
      expect(geometry.right).toBeLessThanOrEqual(geometry.viewport + 1);
      expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewport + 1);

      if (source === 'all') {
        await expect(dialog.locator('[data-provider-section="claude"]')).toContainText('Claude');
        await expect(dialog.locator('[data-provider-section="codex"]')).toContainText('Codex');
        await expect(dialog.locator('[data-provider-section] .mcw-herobar')).toHaveCount(2);
        if (viewport.width === 390) {
          await expect(dialog.locator('.current-week-provider-section .modal-current-week').first())
            .toHaveCSS('overflow-y', 'auto');
        }
      }

      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden();
      await expect(hero).toBeFocused();
    }

    expect(browserErrors).toEqual([]);
  });
}

test('Current Week and Share stay frozen to the opening source', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await selectSource(page, 'claude');
  await page.locator('[data-hero-strip]').click();

  const currentWeek = page.getByRole('dialog', { name: 'Current Cycle — per-percent milestones' });
  await expect(currentWeek).toBeVisible();

  await page.locator('.source-seg[data-source="codex"]').evaluate((node: HTMLElement) => node.click());
  await expect(page.locator('.source-seg[data-source="codex"]')).toHaveClass(/is-active/);
  await expect(currentWeek).toBeVisible();
  // Both providers now correctly name a cycle. The frozen modal's provider
  // identity, not a differing title, proves that the source did not change.
  await expect(currentWeek.locator('.modal-current-week')).toHaveAttribute('data-source', 'claude');

  // The share panel's current-week artifact covers the subscription week,
  // whereas this milestone navigator steps reset-defined billing cycles.
  await currentWeek.getByRole('button', { name: /Share Current week report/i }).click();
  const share = page.getByRole('dialog', { name: 'Share Current week report' });
  await expect(share).toBeVisible();
  await expect(share.locator('.share-modal-source')).toHaveAccessibleName('Source: Claude');
});

test('Current Week restores keyboard focus while an earlier pointer candidate is pending', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await page.evaluate(() => {
    const nativeSetTimeout = window.setTimeout;
    window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
      if (timeout === 0) return 0;
      return nativeSetTimeout(handler, timeout, ...args);
    }) as typeof window.setTimeout;
    try {
      const source = document.querySelector<HTMLElement>('.source-seg[data-source="codex"]');
      if (!source) throw new Error('Codex source selector is missing');
      source.click();
    } finally {
      window.setTimeout = nativeSetTimeout;
    }
  });

  await expect(page.locator('.source-seg[data-source="codex"]')).toHaveClass(/is-active/);
  const hero = page.locator('[data-hero-strip]');
  await hero.focus();
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Current Cycle — per-percent milestones' });
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(hero).toBeFocused();
});
