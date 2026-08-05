import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

const MATRIX = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
] as const;

const FIXTURE = JSON.parse(readFileSync(
  new URL('../__tests__/fixtures/envelope.json', import.meta.url),
  'utf8',
)) as Record<string, any>;

const EVIDENCE_DIR = process.env.ISSUE_469_EVIDENCE_DIR
  ?? 'e2e/.runtime/issue-469-evidence';
const PHASE = process.env.ISSUE_469_CAPTURE_PHASE ?? 'acceptance';

async function installIdleTodayReport(page: Page) {
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
    Object.defineProperty(window, 'EventSource', {
      configurable: true,
      value: StableEventSource,
    });
  });

  await page.route('**/api/data', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json() as Record<string, any>;
    const report = structuredClone(FIXTURE.cache_report);
    const seedDay = report.days[0];
    report.days = Array.from({ length: 14 }, (_, index) => ({
      ...seedDay,
      date: `2026-05-${String(20 - index).padStart(2, '0')}`,
      observed: true,
    }));
    report.days[0] = {
      ...report.days[0],
      input_tokens: 0,
      output_tokens: 0,
      cache_creation_tokens: 0,
      cache_read_tokens: 0,
      saved_usd: 0,
      wasted_usd: 0,
      net_usd: 0,
      observed: false,
      anomaly_triggered: false,
      anomaly_reasons: [],
      anomaly_unevaluated: ['net_negative', 'cache_drop'],
    };
    report.today = { ...report.today, ...report.days[0] };
    envelope.cache_report = report;
    await route.fulfill({ response, json: envelope });
  });
}

for (const viewport of MATRIX) {
  test(`Cache Report charts announce one window at ${viewport.width}x${viewport.height} (#469)`, async ({ page }) => {
    await installIdleTodayReport(page);
    await page.setViewportSize(viewport);
    await page.goto('/');

    const open = page.getByRole('button', { name: 'Open Cache Report' });
    await open.scrollIntoViewIfNeeded();
    await open.click();

    const dialog = page.getByRole('dialog', { name: 'Cache Report' });
    await expect(dialog).toBeVisible();
    const sparkline = dialog.locator('svg.cr-spark');
    const netBars = dialog.locator('.crm-chart-frame.netbars svg');
    await expect(sparkline).toBeVisible();
    await expect(netBars).toBeVisible();

    const labels = {
      sparkline: await sparkline.getAttribute('aria-label'),
      netBars: await netBars.getAttribute('aria-label'),
    };
    mkdirSync(EVIDENCE_DIR, { recursive: true });
    writeFileSync(
      `${EVIDENCE_DIR}/${PHASE}-${viewport.width}x${viewport.height}-labels.json`,
      `${JSON.stringify(labels, null, 2)}\n`,
    );
    await page.screenshot({
      path: `${EVIDENCE_DIR}/${PHASE}-${viewport.width}x${viewport.height}.png`,
      fullPage: true,
    });

    expect(labels).toEqual({
      sparkline: 'Cache hit % timeline, 14 days, 13 measured',
      netBars: 'Per-day net dollar chart, 14 days, 13 measured',
    });

    const geometry = await dialog.evaluate((node) => ({
      dialogRight: node.getBoundingClientRect().right,
      viewportWidth: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
    }));
    expect(geometry.dialogRight).toBeLessThanOrEqual(geometry.viewportWidth + 1);
    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  });
}
