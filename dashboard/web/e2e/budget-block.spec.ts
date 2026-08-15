import { expect, test, type Page } from '@playwright/test';

// #556 S5 (spec §6.1) — an EXECUTABLE regression test for the Forecast footer
// strings and the configured-budget block.
//
// Before this file, no committed Playwright spec read the Forecast footer at
// all. The archived UI-QA reports did, but a report is evidence and not a
// regression test — which is exactly the hole that let S4's F7 deletion pass
// vitest and be caught only after the merge.
//
// What this spec covers, and what it deliberately does not: the e2e fixture
// configures NO budget and seeds no decorated provider, so the states it can
// reach are the footer split and the `provider_budget_unset` disposition with
// its provider-correct command. The configured figures, the chip rows and their
// effect qualifiers need a SEEDED store, and the session's browser gate carries
// those. Nothing here is conditional on what the fixture happens to contain: a
// test that skips itself when the state is absent asserts nothing.

const MATRIX = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
] as const;

async function selectSource(page: Page, source: 'claude' | 'codex' | 'all') {
  const segment = page.locator(`.source-seg[data-source="${source}"]`);
  if (await segment.isVisible()) await segment.click();
  else await segment.evaluate((node: HTMLElement) => node.click());
  await expect(segment).toHaveClass(/is-active/);
}

for (const viewport of MATRIX) {
  test(`forecast footer and budget block at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    const browserErrors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') browserErrors.push(message.text());
    });
    page.on('pageerror', (error) => browserErrors.push(error.message));

    await page.setViewportSize(viewport);
    await page.goto('/');

    // ---- Claude tab -----------------------------------------------------
    await selectSource(page, 'claude');
    const claudePanel = page.locator('#panel-forecast');
    await expect(claudePanel).toBeVisible();
    // The two quota-ceiling rows qualify the PROJECTION and stay in the
    // forecast footer.
    const claudeForecastFoot = claudePanel.locator('.fc-body > .fc-budget-foot');
    await expect(claudeForecastFoot).toContainText('Budget ≤100%');
    await expect(claudeForecastFoot).toContainText('Budget ≤90%');
    // `Budget pace` is a CONFIGURED-BUDGET quantity and left this footer.
    await expect(claudeForecastFoot).not.toContainText('Budget pace');

    const claudeBudget = claudePanel.locator('[data-budget-section="claude"]');
    await expect(claudeBudget).toHaveCount(1);
    await expect(claudeBudget).toHaveAttribute('role', 'region');
    await expect(claudeBudget).toContainText('No budget set.');
    await expect(claudeBudget).toContainText('cctally budget set <amount>');
    await expect(claudeBudget).not.toContainText('--vendor codex');

    // ---- Codex tab ------------------------------------------------------
    await selectSource(page, 'codex');
    const codexPanel = page.locator('#panel-forecast');
    const codexForecastFoot = codexPanel.locator('.fc-body > .fc-budget-foot');
    // `Confidence` qualifies the projection and stays.
    await expect(codexForecastFoot).toContainText('Confidence');
    await expect(codexForecastFoot).not.toContainText('Budget pace');
    const codexBudget = codexPanel.locator('[data-budget-section="codex"]');
    await expect(codexBudget).toHaveCount(1);
    await expect(codexBudget).toContainText('cctally budget set <amount> --vendor codex');

    // ---- All ------------------------------------------------------------
    await selectSource(page, 'all');
    const allPanel = page.locator('#panel-forecast');
    // Two forecast sections and two budget sections, budget cards adjacent.
    await expect(allPanel.locator('[data-provider-section]')).toHaveCount(2);
    const allBudgets = allPanel.locator('[data-budget-section]');
    await expect(allBudgets).toHaveCount(2);
    await expect(allBudgets.nth(0)).toHaveAttribute('data-budget-section', 'claude');
    await expect(allBudgets.nth(1)).toHaveAttribute('data-budget-section', 'codex');
    // No composed budget figure anywhere: exactly two provider-labelled cards.
    await expect(allPanel).not.toContainText('Combined budget');
    for (const section of await allPanel.locator('[data-provider-section]').all()) {
      await expect(section.locator('.fc-budget-foot')).not.toContainText('Budget pace');
    }

    // The heading ids are surface-qualified so the panel and its modal can be
    // mounted at once without colliding.
    await expect(page.locator('#budget-panel-claude-heading')).toHaveCount(1);
    await expect(page.locator('#budget-panel-codex-heading')).toHaveCount(1);

    // ---- All, modal -----------------------------------------------------
    await page.getByRole('button', { name: 'Open Forecast' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    const modalBudgets = dialog.locator('[data-budget-section]');
    await expect(modalBudgets).toHaveCount(2);
    for (const section of await modalBudgets.all()) {
      await expect(section).toHaveAttribute('data-surface', 'modal');
    }
    await expect(page.locator('#budget-modal-claude-heading')).toHaveCount(1);
    await expect(page.locator('#budget-modal-codex-heading')).toHaveCount(1);
    // The panel's own ids are still exactly one each while both are mounted.
    await expect(page.locator('#budget-panel-claude-heading')).toHaveCount(1);
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();

    // No horizontal overflow at either viewport.
    const geometry = await page.evaluate(() => ({
      viewport: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
    }));
    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewport + 1);
    expect(browserErrors).toEqual([]);
  });
}
