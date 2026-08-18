import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';
import { fulfilJson } from './utils';

const DECORATED_FIXTURE = JSON.parse(readFileSync(
  new URL('../../../tests/fixtures/dashboard/all-combined-decorated/golden-data.json', import.meta.url),
  'utf8',
)) as Record<string, any>;

const OK_FIXTURE = JSON.parse(readFileSync(
  new URL('../../../tests/fixtures/dashboard/ok/golden-data.json', import.meta.url),
  'utf8',
)) as Record<string, any>;

const WARNING_DOMAINS = [
  ['hero', 'Hero unavailable'],
  ['daily', 'Daily unavailable'],
  ['weekly', 'Weekly unavailable'],
  ['monthly', 'Monthly unavailable'],
  ['sessions', 'Sessions unavailable'],
  ['projects', 'Projects unavailable'],
  ['quota', 'Quota unavailable'],
  ['budget', 'Budget unavailable'],
  ['forensics', 'Forensics unavailable'],
  ['alerts', 'Alerts unavailable'],
] as const;

async function freezeEventStream(page: Page) {
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
      listeners = new Map<string, Array<(ev: MessageEvent) => any>>();

      constructor(url: string | URL) {
        this.url = String(url);
        // #583 S3 §7 — the client's bootstrap `fetch('/api/data')` is gone, so
        // this page receives its envelope ONLY as an `update` event. A stub
        // whose `addEventListener` was a no-op therefore never renders
        // anything at all. It still suppresses live ticks, which is what makes
        // the fixture stable; it just delivers exactly ONE frame first, read
        // through `/api/data` so this spec's own route handler keeps shaping
        // it exactly as before.
        if (!this.url.includes('/api/events')) return;
        void fetch('/api/data')
          .then((r) => r.json())
          .then((payload) => {
            const ev = new MessageEvent('update', { data: JSON.stringify(payload) });
            for (const fn of this.listeners.get('update') ?? []) fn(ev);
          })
          // Never swallow this. The stub IS the page's only source of
          // data, so a failed fixture delivery renders a blank dashboard
          // and every later assertion fails on a missing element instead
          // of on the real cause.
          .catch((err) => { console.error(`fixture delivery failed: ${err}`); });
      }

      addEventListener(type: string, fn: (ev: MessageEvent) => any) {
        const list = this.listeners.get(type) ?? [];
        list.push(fn);
        this.listeners.set(type, list);
      }

      removeEventListener(type: string, fn: (ev: MessageEvent) => any) {
        this.listeners.set(
          type, (this.listeners.get(type) ?? []).filter((f) => f !== fn),
        );
      }

      dispatchEvent() { return true; }
      close() { this.readyState = StableEventSource.CLOSED; }
    }
    Object.defineProperty(window, 'EventSource', { configurable: true, value: StableEventSource });
  });
}

async function selectSource(page: Page, source: 'claude' | 'codex' | 'all') {
  const segment = page.locator(`.source-seg[data-source="${source}"]`);
  if (await segment.isVisible()) await segment.click();
  else await segment.evaluate((node: HTMLElement) => node.click());
  await expect(segment).toHaveClass(/is-active/);
}

function materializeFixture(value: any, live: Record<string, any>): any {
  if (Array.isArray(value)) return value.map((item) => materializeFixture(item, live));
  if (value != null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, materializeFixture(item, live)]),
    );
  }
  if (value === '__DOCTOR_BLOCK__') return live.doctor;
  if (value === '__UPDATE_BLOCK__') return live.update;
  if (value === '__NOW__') return live.generated_at;
  if (value === '__VER__') return live.data_version;
  if (value === '__LABEL__') return 'fresh';
  return value;
}

async function serveDecoratedFixture(page: Page) {
  await freezeEventStream(page);
  await page.route('**/api/data', async (route) => {
    const response = await route.fetch();
    const live = await response.json() as Record<string, any>;
    await fulfilJson(route, response, materializeFixture(DECORATED_FIXTURE, live));
  });
}

async function serveFixture(page: Page, fixture: Record<string, any>) {
  await freezeEventStream(page);
  await page.route('**/api/data', async (route) => {
    const response = await route.fetch();
    const live = await response.json() as Record<string, any>;
    await fulfilJson(route, response, materializeFixture(fixture, live));
  });
}

test('#561 — the 320px Claude usage zone contains both metric blocks', async ({ page }) => {
  await serveFixture(page, OK_FIXTURE);
  await page.setViewportSize({ width: 320, height: 900 });
  await page.goto('/');
  await selectSource(page, 'claude');

  const usage = page.locator('.hero-usage');
  await expect(usage.locator('.hu-block')).toHaveCount(2);
  const geometry = await usage.evaluate((node) => ({
    clientWidth: node.clientWidth,
    scrollWidth: node.scrollWidth,
    blocks: Array.from(node.querySelectorAll<HTMLElement>('.hu-block')).map((block) => ({
      clientWidth: block.clientWidth,
      scrollWidth: block.scrollWidth,
    })),
  }));

  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth);
  expect(geometry.blocks.every((block) => block.scrollWidth <= block.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(320);
});

for (const viewport of [
  { width: 390, height: 844 },
  { width: 320, height: 900 },
] as const) {
  test(`#562 — decorated All cards stay reachable without hiding the board at ${viewport.width}px`, async ({ page }) => {
    await serveDecoratedFixture(page);
    await page.setViewportSize(viewport);
    await page.goto('/');
    await selectSource(page, 'all');

    const rail = page.getByTestId('account-hero-cards');
    const cards = page.getByTestId('account-hero-card');
    await expect(cards).toHaveCount(3);
    await expect(page.getByTestId('account-hero-caption')).toContainText('Claude accounts');

    const before = await rail.evaluate((node) => ({
      clientWidth: node.clientWidth,
      scrollWidth: node.scrollWidth,
      overflowX: getComputedStyle(node).overflowX,
      cardTops: Array.from(node.querySelectorAll<HTMLElement>('.account-hero-card'))
        .map((card) => card.getBoundingClientRect().top),
      firstPanelTop: document.querySelector('.dash-grid .panel')?.getBoundingClientRect().top,
    }));
    expect(before.overflowX).toBe('auto');
    expect(before.scrollWidth).toBeGreaterThan(before.clientWidth);
    expect(Math.max(...before.cardTops) - Math.min(...before.cardTops)).toBeLessThanOrEqual(1);
    expect(before.firstPanelTop).toBeLessThan(viewport.height);

    await rail.evaluate((node) => { node.scrollLeft = node.scrollWidth; });
    await expect.poll(() => rail.evaluate((node) => {
      const railRect = node.getBoundingClientRect();
      const allCards = node.querySelectorAll<HTMLElement>('.account-hero-card');
      const last = allCards.item(allCards.length - 1);
      if (last == null) return false;
      const lastRect = last.getBoundingClientRect();
      return lastRect.left >= railRect.left - 1 && lastRect.right <= railRect.right + 1;
    })).toBe(true);
  });
}

test('#563 — every 390px domain warning keeps a visible state word', async ({ page }) => {
  await freezeEventStream(page);
  await page.setViewportSize({ width: 390, height: 844 });
  let activeWarning = WARNING_DOMAINS[0];
  await page.route('**/api/data', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json() as Record<string, any>;
    envelope.sources.codex = {
      ...envelope.sources.codex,
      availability: 'partial',
      last_success_at: envelope.sources.codex.last_success_at ?? envelope.generated_at,
      warnings: [{
        code: `${activeWarning[0]}_unavailable`,
        domain: activeWarning[0],
        message: `${activeWarning[1]}.`,
      }],
    };
    await fulfilJson(route, response, envelope);
  });

  for (const warning of WARNING_DOMAINS) {
    activeWarning = warning;
    await page.goto('/');
    await selectSource(page, 'codex');
    const chip = page.getByTestId('source-status-chip');
    await expect(chip.locator('.source-status-label--full')).toBeHidden();
    await expect(chip.locator('.source-status-label--full')).toHaveText(warning[1]);
    await expect(chip.locator('.source-status-label--compact')).toBeVisible();
    await expect(chip.locator('.source-status-label--compact')).toHaveText('Unavailable');
    const geometry = await chip.evaluate((node) => ({
      clientWidth: node.clientWidth,
      scrollWidth: node.scrollWidth,
      right: node.getBoundingClientRect().right,
    }));
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
    expect(geometry.right).toBeLessThanOrEqual(391);
  }
});
