import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';
import { fulfilJson } from './utils';

const DECORATED_FIXTURE = JSON.parse(readFileSync(
  new URL('../../../tests/fixtures/dashboard/all-combined-decorated/golden-data.json', import.meta.url),
  'utf8',
)) as Record<string, any>;

const COMBINED_FIXTURE = JSON.parse(readFileSync(
  new URL('../../../tests/fixtures/dashboard/all-combined/golden-data.json', import.meta.url),
  'utf8',
)) as Record<string, any>;

const OK_FIXTURE = JSON.parse(readFileSync(
  new URL('../../../tests/fixtures/dashboard/ok/golden-data.json', import.meta.url),
  'utf8',
)) as Record<string, any>;

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
    Object.defineProperty(window, 'SharedWorker', { configurable: true, value: undefined });
    Object.defineProperty(window, 'EventSource', { configurable: true, value: StableEventSource });
  });
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

async function serveFixture(
  page: Page,
  fixture: Record<string, any>,
  mutate?: (envelope: Record<string, any>) => void,
) {
  await freezeEventStream(page);
  await page.route('**/api/data', async (route) => {
    const response = await route.fetch();
    const live = await response.json() as Record<string, any>;
    const envelope = materializeFixture(fixture, live) as Record<string, any>;
    mutate?.(envelope);
    await fulfilJson(route, response, envelope);
  });
}

async function selectSource(page: Page, source: 'claude' | 'codex' | 'all') {
  const segment = page.locator(`.source-seg[data-source="${source}"]`);
  await segment.click();
  await expect(segment).toHaveClass(/is-active/);
}

const LARGE_AMOUNT_CASES = [
  {
    source: 'claude' as const,
    fixture: OK_FIXTURE,
    mutate: (envelope: Record<string, any>) => {
      envelope.current_week.spent_usd = 3947.86;
      envelope.sources.claude.data.hero.cost_usd = 3947.86;
      envelope.sources.claude.data.hero.current_week.spent_usd = 3947.86;
    },
  },
  {
    source: 'codex' as const,
    fixture: DECORATED_FIXTURE,
    mutate: (envelope: Record<string, any>) => {
      envelope.sources.codex.data.hero.cost_usd = 3947.86;
    },
  },
  {
    source: 'all' as const,
    fixture: COMBINED_FIXTURE,
    mutate: (envelope: Record<string, any>) => {
      envelope.sources.all.data.combined.cost_usd = 3947.86;
    },
  },
];

for (const { source, fixture, mutate } of LARGE_AMOUNT_CASES) {
  test(`#596 — ${source} keeps a four-figure hero amount complete at 320px`, async ({ page }) => {
    await serveFixture(page, fixture, mutate);
    await page.setViewportSize({ width: 320, height: 812 });
    await page.goto('/');
    await selectSource(page, source);

    const amount = page.locator('.hero-spent .hs-big');
    await expect(amount).toHaveText('$3947.86');
    const geometry = await amount.evaluate((node) => {
      const range = document.createRange();
      range.selectNodeContents(node);
      const text = range.getBoundingClientRect();
      const box = node.getBoundingClientRect();
      const strip = node.closest('.hero-strip')!.getBoundingClientRect();
      return {
        boxWidth: box.width,
        clientWidth: node.clientWidth,
        scrollWidth: node.scrollWidth,
        textRight: text.right,
        stripRight: strip.right,
        documentClientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
      };
    });

    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
    expect(geometry.textRight).toBeLessThanOrEqual(geometry.stripRight + 1);
    expect(geometry.documentScrollWidth).toBeLessThanOrEqual(geometry.documentClientWidth);
  });
}

test('#593 — the Daily footer is fully visible at the default panel scroll position', async ({ page }) => {
  await serveFixture(page, COMBINED_FIXTURE);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await selectSource(page, 'all');

  const body = page.locator('#panel-daily .panel-body');
  const footer = page.locator('#panel-daily .daily-foot');
  await expect(footer).toBeVisible();
  const geometry = await body.evaluate((node) => {
    const footerNode = node.querySelector('.daily-foot')!;
    const bodyRect = node.getBoundingClientRect();
    const footerRect = footerNode.getBoundingClientRect();
    return {
      scrollTop: node.scrollTop,
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      bodyBottom: bodyRect.bottom,
      footerBottom: footerRect.bottom,
      overflowY: getComputedStyle(node).overflowY,
    };
  });

  expect(geometry.overflowY).toBe('auto');
  expect(geometry.scrollTop).toBe(0);
  expect(geometry.scrollHeight).toBeLessThanOrEqual(geometry.clientHeight + 1);
  expect(geometry.footerBottom).toBeLessThanOrEqual(geometry.bodyBottom + 1);
});

test('#573 — every All range note stays readable while Alerts recovers body height', async ({ page }) => {
  await serveFixture(page, COMBINED_FIXTURE);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await selectSource(page, 'all');

  const notes = page.locator('.panel-range-note');
  await expect(notes).toHaveCount(7);
  for (const note of await notes.all()) {
    await expect(note).toBeVisible();
    await expect(note).not.toHaveText('');
  }

  const desktop = await page.locator('#panel-alerts').evaluate((panel) => {
    const body = panel.querySelector<HTMLElement>('#panel-alerts-body')!;
    const note = panel.querySelector<HTMLElement>('.panel-range-note')!;
    return {
      bodyClientHeight: body.clientHeight,
      noteHeight: note.getBoundingClientRect().height,
      panelHeight: panel.getBoundingClientRect().height,
    };
  });
  expect(desktop.panelHeight).toBe(200);
  expect(desktop.bodyClientHeight).toBeGreaterThanOrEqual(84);
  expect(desktop.noteHeight).toBeGreaterThanOrEqual(12);

  await page.setViewportSize({ width: 390, height: 844 });
  const mobile = await notes.evaluateAll((elements) => ({
    documentClientWidth: document.documentElement.clientWidth,
    documentScrollWidth: document.documentElement.scrollWidth,
    notes: elements.map((node) => {
      const el = node as HTMLElement;
      return {
        text: el.textContent,
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        height: el.getBoundingClientRect().height,
      };
    }),
  }));
  expect(mobile.documentScrollWidth).toBeLessThanOrEqual(mobile.documentClientWidth);
  expect(mobile.notes).toHaveLength(7);
  for (const note of mobile.notes) {
    expect(note.text?.trim()).not.toBe('');
    expect(note.scrollWidth).toBeLessThanOrEqual(note.clientWidth + 1);
    expect(note.height).toBeGreaterThanOrEqual(12);
  }
});
