import { mkdirSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

const MATRIX = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
] as const;

const LONG_HERO_WARNING =
  'Codex cycle accounting cannot be combined because its retained native reset evidence is incomplete for this exact provider period.';

async function selectSource(page: Page, source: 'claude' | 'codex' | 'all') {
  const segment = page.locator(`.source-seg[data-source="${source}"]`);
  if (await segment.isVisible()) await segment.click();
  else await segment.evaluate((node: HTMLElement) => node.click());
  await expect(segment).toHaveClass(/is-active/);
}

function screenshotPath(viewport: typeof MATRIX[number], name: string) {
  const phase = process.env.ISSUE_329_CAPTURE_PHASE ?? 'acceptance';
  const dir = 'e2e/.runtime/issue-329-task-b-evidence';
  mkdirSync(dir, { recursive: true });
  return `${dir}/${phase}-${viewport.width}x${viewport.height}-${name}.png`;
}

async function assertPageGeometry(page: Page) {
  const geometry = await page.evaluate(() => ({
    viewportWidth: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    duplicateIds: Array.from(document.querySelectorAll('[id]'))
      .map((node) => node.id)
      .filter((id, index, ids) => ids.indexOf(id) !== index),
  }));
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.duplicateIds).toEqual([]);
}

async function transformInitialEnvelope(
  page: Page,
  transform: (envelope: Record<string, any>) => void,
) {
  await page.addInitScript(() => {
    class StableEventSource {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readonly url: string;
      readonly withCredentials = false;
      readyState = 1;
      onopen: ((this: EventSource, ev: Event) => any) | null = null;
      onmessage: ((this: EventSource, ev: MessageEvent) => any) | null = null;
      onerror: ((this: EventSource, ev: Event) => any) | null = null;

      constructor(url: string | URL) {
        this.url = String(url);
      }

      addEventListener() {}
      removeEventListener() {}
      dispatchEvent() { return true; }
      close() { this.readyState = StableEventSource.CLOSED; }
    }
    Object.defineProperty(window, 'EventSource', { configurable: true, value: StableEventSource });
  });
  await page.route('**/api/data', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json() as Record<string, any>;
    transform(envelope);
    await route.fulfill({ response, json: envelope });
  });
}

function removeCodexFiveHour(quota: Record<string, any> | undefined) {
  if (quota == null) return;
  quota.histories = (quota.histories ?? []).filter((row: Record<string, any>) => row.window_minutes !== 300);
  quota.blocks = (quota.blocks ?? []).filter((row: Record<string, any>) => row.window_minutes !== 300);
}

for (const viewport of MATRIX) {
  test(`partial source status stays readable at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    const browserErrors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') browserErrors.push(message.text());
    });
    page.on('pageerror', (error) => browserErrors.push(error.message));

    await page.setViewportSize(viewport);
    await page.goto('/');
    await selectSource(page, 'codex');

    const chip = page.getByTestId('source-status-chip');
    await expect(chip).toHaveClass(/is-degraded/);
    await expect(chip).toHaveAttribute('title', /lack project metadata/);
    await expect(chip).toHaveAttribute('aria-label', /lack project metadata/);
    if (viewport.width <= 640) {
      await expect(chip.locator('.source-status-label--compact')).toBeVisible();
      await expect(chip.locator('.source-status-label--compact')).toHaveText('Projects');
      await expect(chip.locator('.source-status-label--full')).toBeHidden();
    } else {
      await expect(chip.locator('.source-status-label--full')).toBeVisible();
      await expect(chip.locator('.source-status-label--full')).toHaveText('Projects partial');
      await expect(chip.locator('.source-status-label--compact')).toBeHidden();
    }

    const chipGeometry = await chip.evaluate((node) => ({
      clientWidth: node.clientWidth,
      scrollWidth: node.scrollWidth,
      right: node.getBoundingClientRect().right,
      viewportWidth: window.innerWidth,
    }));
    expect(chipGeometry.scrollWidth).toBeLessThanOrEqual(chipGeometry.clientWidth + 1);
    expect(chipGeometry.right).toBeLessThanOrEqual(chipGeometry.viewportWidth + 1);
    await assertPageGeometry(page);
    await page.screenshot({ path: screenshotPath(viewport, 'partial-source-status'), fullPage: true });
    expect(browserErrors).toEqual([]);
  });

  test(`native Codex five-hour facts remain visible at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/');
    await selectSource(page, 'codex');

    await expect(page.getByTestId('hero-five-hour')).toBeVisible();
    await expect(page.getByTestId('hero-five-hour')).toContainText(/\d+%/);
    const codexBlocks = page.locator('#panel-blocks');
    await expect(codexBlocks.getByRole('heading', { name: /Blocks/ })).not.toContainText('optional');
    await expect(codexBlocks.locator('.blocks-row').first()).toBeVisible();
    await codexBlocks.scrollIntoViewIfNeeded();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: screenshotPath(viewport, 'codex-native-five-hour'), fullPage: true });

    await selectSource(page, 'all');
    const allBlocks = page.locator('#panel-blocks');
    await expect(allBlocks.locator('.source-chip--codex').first()).toHaveText('Codex');
    await assertPageGeometry(page);
    await page.screenshot({ path: screenshotPath(viewport, 'all-five-hour-ownership'), fullPage: true });
  });

  test(`optional Codex five-hour absence and long All warning stay bounded at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await transformInitialEnvelope(page, (envelope) => {
      removeCodexFiveHour(envelope.sources?.codex?.data?.quota);
      removeCodexFiveHour(envelope.sources?.all?.data?.providers?.codex?.quota);
      envelope.sources.all.availability = 'partial';
      envelope.sources.all.warnings = [{
        code: 'codex_cycle_unavailable',
        domain: 'hero',
        message: LONG_HERO_WARNING,
      }];
      envelope.sources.all.data.combined = null;
    });

    await page.setViewportSize(viewport);
    await page.goto('/');
    await selectSource(page, 'codex');
    await expect(page.getByTestId('hero-five-hour')).toHaveCount(0);
    const codexBlocks = page.locator('#panel-blocks');
    await expect(codexBlocks.getByRole('heading', { name: /Blocks/ })).toContainText('optional 5h · current cycle');
    await expect(codexBlocks).toContainText('the 7-day Codex cycle remains available');
    await page.screenshot({ path: screenshotPath(viewport, 'codex-no-five-hour'), fullPage: true });

    await page.evaluate(() => window.scrollTo(0, 0));
    await selectSource(page, 'all');
    const warning = page.getByTestId('shared-hero-warning');
    await expect(warning).toBeVisible();
    await expect(warning).toHaveText('Combined unavailable');
    await expect(warning).toHaveAttribute('title', LONG_HERO_WARNING);
    await expect(warning).toHaveAttribute('aria-label', `Combined totals unavailable: ${LONG_HERO_WARNING}`);
    await expect(page.getByTestId('shared-hero-spent')).not.toContainText(LONG_HERO_WARNING);
    await assertPageGeometry(page);
    await page.screenshot({ path: screenshotPath(viewport, 'all-bounded-warning'), fullPage: true });
  });
}
