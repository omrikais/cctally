import { mkdirSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

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

async function openPeriod(page: Page, label: 'Daily' | 'Weekly' | 'Monthly') {
  await page.getByRole('button', { name: `Open ${label}` }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  return dialog;
}

function screenshotPath(viewport: typeof MATRIX[number], name: string) {
  const phase = process.env.ISSUE_329_CAPTURE_PHASE ?? 'acceptance';
  const dir = 'e2e/.runtime/issue-329-evidence';
  mkdirSync(dir, { recursive: true });
  return `${dir}/${phase}-${viewport.width}x${viewport.height}-${name}.png`;
}

for (const viewport of MATRIX) {
  test(`provider-native period vocabulary at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    const browserErrors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') browserErrors.push(message.text());
    });
    page.on('pageerror', (error) => browserErrors.push(error.message));

    await page.setViewportSize(viewport);
    await page.goto('/');

    await selectSource(page, 'codex');
    for (const label of ['Daily', 'Monthly'] as const) {
      const trigger = page.getByRole('button', { name: `Open ${label}` });
      const dialog = await openPeriod(page, label);
      await dialog.screenshot({ path: screenshotPath(viewport, `codex-${label.toLowerCase()}`) });
      for (const tokenLabel of ['Input', 'Cached input', 'Output', 'Reasoning', 'Total']) {
        await expect(dialog.getByText(tokenLabel, { exact: true })).toBeVisible();
      }
      await expect(dialog.getByText('Cache+', { exact: true })).toHaveCount(0);
      await expect(dialog.getByText('Cache-read', { exact: true })).toHaveCount(0);
      if (label === 'Daily') {
        await page.locator('.source-seg[data-source="all"]').evaluate((node: HTMLElement) => node.click());
        await expect(dialog).toHaveAttribute('data-source', 'codex');
      }
      await page.keyboard.press('Escape');
      await expect(trigger).toBeFocused();
      if (label === 'Daily') await selectSource(page, 'codex');
    }

    const codexWeekly = await openPeriod(page, 'Weekly');
    await codexWeekly.screenshot({ path: screenshotPath(viewport, 'codex-weekly') });
    await expect(codexWeekly.getByRole('heading')).toHaveText(/Weekly · last \d+ cycles/);
    await expect(codexWeekly.locator('[data-col="label"]')).toContainText('Cycle');
    await expect(codexWeekly.getByRole('region', { name: 'Cost by cycle' })).toBeVisible();
    await expect(codexWeekly.getByText(/Reset cycle:/)).toBeVisible();
    await expect(codexWeekly.getByText(/vs prior cycle/)).toBeVisible();
    await page.keyboard.press('Escape');

    await selectSource(page, 'all');
    const allWeekly = await openPeriod(page, 'Weekly');
    const codexRow = allWeekly.getByRole('gridcell', { name: 'Codex', exact: true }).first().locator('..');
    await codexRow.click();
    await allWeekly.screenshot({ path: screenshotPath(viewport, 'all-weekly-codex-row') });
    await expect(allWeekly.getByRole('heading')).toHaveText(/Weekly · \d+ provider periods/);
    await expect(allWeekly.locator('[data-col="label"]')).toContainText('Provider period');
    await expect(allWeekly.getByText(/vs prior provider period/)).toBeVisible();
    await page.keyboard.press('Escape');

    await selectSource(page, 'codex');
    await page.getByRole('button', { name: 'Open Projects' }).click();
    const projects = page.getByRole('dialog', { name: /Projects/ });
    await expect(projects).toBeVisible();
    const projectRows = projects.getByTestId('projects-table-row');
    const firstProject = projectRows.filter({ hasText: 'repo (1)' });
    const secondProject = projectRows.filter({ hasText: 'repo (2)' });
    await expect(firstProject).toHaveCount(1);
    await expect(secondProject).toHaveCount(1);
    await projects.screenshot({ path: screenshotPath(viewport, 'codex-partial-projects') });

    const detailUrls: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/source/codex/project/')) detailUrls.push(request.url());
    });
    for (const [row, label] of [[firstProject, 'repo (1)'], [secondProject, 'repo (2)']] as const) {
      await row.click();
      const detail = page.getByRole('dialog', { name: 'Project detail' });
      await expect(detail).toBeVisible();
      await expect(detail.getByText(label, { exact: true })).toBeVisible();
      await expect(detail.getByTestId('source-detail-error')).toHaveCount(0);
      await detail.getByRole('button', { name: 'Close' }).click();
      await expect(detail).toBeHidden();
    }
    expect(new Set(detailUrls).size).toBe(2);
    expect(detailUrls.every((url) => /\/project\/project%3A[A-Za-z0-9_-]{43}\?/.test(url))).toBe(true);
    expect(detailUrls.every((url) => !/root-secret|\/synthetic\/|workspace|personal/.test(decodeURIComponent(url)))).toBe(true);
    await page.keyboard.press('Escape');

    await selectSource(page, 'claude');
    const claudeWeekly = await openPeriod(page, 'Weekly');
    await claudeWeekly.screenshot({ path: screenshotPath(viewport, 'claude-weekly') });
    await expect(claudeWeekly.getByRole('heading')).toHaveText(/Weekly · last \d+ weeks/);
    await expect(claudeWeekly.locator('[data-col="label"]')).toContainText('Week');
    await expect(claudeWeekly.getByText(/Subscription window:/)).toBeVisible();
    for (const tokenLabel of ['Input', 'Output', 'Cache+', 'Cache-read', 'Total']) {
      await expect(claudeWeekly.getByText(tokenLabel, { exact: true })).toBeVisible();
    }

    const geometry = await claudeWeekly.evaluate((node) => ({
      left: node.getBoundingClientRect().left,
      right: node.getBoundingClientRect().right,
      viewport: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
    }));
    expect(geometry.left).toBeGreaterThanOrEqual(-1);
    expect(geometry.right).toBeLessThanOrEqual(geometry.viewport + 1);
    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewport + 1);
    expect(browserErrors).toEqual([]);
  });
}
