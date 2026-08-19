import { expect, test, type Page } from '@playwright/test';
import { fulfilJson } from './utils';

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
        if (!this.url.includes('/api/events')) return;
        void fetch('/api/data')
          .then((response) => response.json())
          .then((payload) => {
            const event = new MessageEvent('update', { data: JSON.stringify(payload) });
            for (const listener of this.listeners.get('update') ?? []) listener(event);
          })
          .catch((error) => { console.error(`fixture delivery failed: ${error}`); });
      }

      addEventListener(type: string, listener: (event: MessageEvent) => any) {
        const listeners = this.listeners.get(type) ?? [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
      }

      removeEventListener(type: string, listener: (event: MessageEvent) => any) {
        this.listeners.set(
          type,
          (this.listeners.get(type) ?? []).filter((candidate) => candidate !== listener),
        );
      }

      dispatchEvent() { return true; }
      close() { this.readyState = StableEventSource.CLOSED; }
    }
    Object.defineProperty(window, 'SharedWorker', { configurable: true, value: undefined });
    Object.defineProperty(window, 'EventSource', {
      configurable: true,
      value: StableEventSource,
    });
  });
}

async function selectAll(page: Page) {
  const segment = page.locator('.source-seg[data-source="all"]');
  if (await segment.isVisible()) await segment.click();
  else await segment.evaluate((node: HTMLElement) => node.click());
  await expect(segment).toHaveClass(/is-active/);
}

async function serveInterleavedBlocks(page: Page) {
  await freezeEventStream(page);
  await page.route('**/api/data', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json() as Record<string, any>;
    envelope.sources.claude.data.quota.blocks = [
      {
        key: 'block:claude-late', source: 'claude',
        start_at: '2026-07-14T15:00:00Z', end_at: '2026-07-14T20:00:00Z',
        anchor: 'recorded', is_active: true, cost_usd: 2,
        models: [], label: 'Claude late',
      },
      {
        key: 'block:claude-early', source: 'claude',
        start_at: '2026-07-14T05:00:00Z', end_at: '2026-07-14T10:00:00Z',
        anchor: 'recorded', is_active: false, cost_usd: 4,
        models: [], label: 'Claude early',
      },
    ];
    const codexQuota = envelope.sources.codex.data.quota;
    const weekly = codexQuota.histories.find(
      (row: Record<string, any>) => row.window_minutes === 10_080,
    ) ?? codexQuota.histories[0];
    codexQuota.histories = [
      ...codexQuota.histories,
      {
        ...weekly,
        key: 'history:codex-five-hour',
        window_minutes: 300,
        model_scoped: false,
      },
    ];
    codexQuota.blocks = [{
      key: 'block:codex-middle', source: 'codex', label: 'Codex middle',
      window_minutes: 300, start_at: '2026-07-14T10:00:00Z',
      end_at: '2026-07-14T15:00:00Z', resets_at: '2026-07-14T15:00:00Z',
      current_percent: 20, orphaned: false, is_active: false, cost_usd: 6,
      model_breakdowns: [],
    }];
    await fulfilJson(route, response, envelope);
  });
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 320, height: 900 },
] as const) {
  test(`#570 — All Blocks interleave and label totals at ${viewport.width}px`, async ({ page }, testInfo) => {
    await serveInterleavedBlocks(page);
    await page.setViewportSize(viewport);
    await page.goto('/');
    await selectAll(page);

    const panel = page.locator('#panel-blocks');
    await expect(panel).toBeVisible();
    const rows = panel.locator('.blocks-row');
    await expect(rows).toHaveCount(3);
    await expect(rows.locator('.label')).toHaveText([
      /Claude.*Claude late/,
      /Codex.*Codex middle/,
      /Claude.*Claude early/,
    ]);
    const footer = panel.locator('.panel-foot');
    await expect(footer).toContainText('$12.00');
    await expect(footer).toContainText('Claude 2 blocks $6.00');
    await expect(footer).toContainText('Codex 1 block $6.00');
    await expect(footer).toContainText('windows shown span');
    await panel.screenshot({
      path: testInfo.outputPath(`issue-570-${viewport.width}-top.png`),
    });

    if (viewport.width === 320) {
      await rows.nth(1).screenshot({
        path: testInfo.outputPath('issue-570-320-middle.png'),
      });
      await rows.nth(2).screenshot({
        path: testInfo.outputPath('issue-570-320-early.png'),
      });
    }

    const width = await panel.evaluate((node) => ({
      client: node.clientWidth,
      scroll: node.scrollWidth,
    }));
    expect(width.scroll).toBeLessThanOrEqual(width.client + 1);
  });
}

test('#616 — doctor remediation flags stay intact at 320px', async ({ page }, testInfo) => {
  await freezeEventStream(page);
  await page.route('**/api/doctor', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 1,
        generated_at: '2026-07-14T16:10:00Z',
        cctally_version: 'test',
        overall: { severity: 'warn', counts: { ok: 0, warn: 3, fail: 0 } },
        categories: [{
          id: 'database', title: 'Database', severity: 'warn',
          counts: { ok: 0, warn: 3, fail: 0 },
          checks: [
            {
              id: 'db.conversations_wal_size', title: 'Conversation WAL',
              severity: 'warn', summary: 'large', details: {},
              remediation: 'Run `cctally db checkpoint --db conversations` to drain the WAL.',
            },
            {
              id: 'codex.metadata', title: 'Codex metadata',
              severity: 'warn', summary: 'incomplete', details: {},
              remediation: 'Run `cctally cache-sync --source codex --rebuild`.',
            },
            {
              id: 'hooks.legacy', title: 'Legacy hooks',
              severity: 'warn', summary: 'detected', details: {},
              remediation: 'Run `cctally setup --migrate-legacy-hooks`.',
            },
          ],
        }],
      }),
    });
  });
  await page.setViewportSize({ width: 320, height: 900 });
  await page.goto('/');
  await page.keyboard.press('d');
  const dialog = page.locator('.doctor-modal-card');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.doctor-modal__flag')).toHaveCount(4);
  await dialog.screenshot({
    path: testInfo.outputPath('issue-616-320.png'),
  });

  const flags = ['--db', '--source', '--rebuild', '--migrate-legacy-hooks'];
  for (const flag of flags) {
    const lineTops = await dialog.evaluate((node, needle) => {
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = walker.currentNode.textContent ?? '';
        const offset = text.indexOf(needle);
        if (offset < 0) continue;
        return Array.from(needle).map((_, index) => {
          const range = document.createRange();
          range.setStart(walker.currentNode, offset + index);
          range.setEnd(walker.currentNode, offset + index + 1);
          return Math.round(range.getBoundingClientRect().top * 10) / 10;
        });
      }
      return [];
    }, flag);
    expect(lineTops, `flag ${flag} must be present`).toHaveLength(flag.length);
    expect(new Set(lineTops).size, `flag ${flag} must occupy one visual line`).toBe(1);
  }
});
