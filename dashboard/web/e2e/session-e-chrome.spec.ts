import { mkdirSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';
import { fulfilJson } from './utils';

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
  await page.route('**/api/data', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json() as Record<string, any>;
    transform(envelope);
    await fulfilJson(route, response, envelope);
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
      await expect(chip.locator('.source-status-label--compact')).toHaveText('Partial');
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
      // #583 S3 §4 — there is ONE copy. `sources.all.data.providers` publishes
      // null for both providers since source schema 10, so the second call
      // here operated on `undefined` and the All tab now reads this same
      // physical entry through `presentationProviders`' fallback.
      removeCodexFiveHour(envelope.sources?.codex?.data?.quota);
      envelope.sources.all.availability = 'partial';
      envelope.sources.all.warnings = [{
        code: 'codex_cycle_unavailable',
        domain: 'hero',
        message: LONG_HERO_WARNING,
      }];
      envelope.sources.all.data.combined = null;
      // #556 S1 §3.5 — the v5 server always names its reason beside a withheld
      // figure, so the browser case exercises the typed diagnostic rather than
      // the legacy warning fallback (which its own unit test covers).
      envelope.sources.all.data.combined_unavailable = {
        code: 'codex_cycle_unavailable',
        message: LONG_HERO_WARNING,
        causes: [{ provider: 'codex', code: 'codex_cycle_unavailable' }],
      };
    });

    await page.setViewportSize(viewport);
    await page.goto('/');
    await selectSource(page, 'codex');
    await expect(page.getByTestId('hero-five-hour')).toHaveCount(0);
    const codexBlocks = page.locator('#panel-blocks');
    // #556 S2 QA — the scope statement moved off the truncating h2 and onto
    // the panel's wrapping sub-line. Same string, different element.
    await expect(codexBlocks.locator('.panel-range-note')).toHaveText('optional 5h · current cycle');
    await expect(codexBlocks).toContainText('the 7-day Codex cycle remains available');
    await page.screenshot({ path: screenshotPath(viewport, 'codex-no-five-hour'), fullPage: true });

    await page.evaluate(() => window.scrollTo(0, 0));
    await selectSource(page, 'all');
    const warning = page.getByTestId('shared-hero-warning');
    await expect(warning).toBeVisible();
    await expect(warning).toHaveText('Combined withheld');
    await expect(warning).toHaveAttribute('title', LONG_HERO_WARNING);
    await expect(warning).toHaveAttribute('aria-label', `Combined total withheld: ${LONG_HERO_WARNING}`);
    // #556 S1 §5 — the reason is now rendered VISIBLY. It previously reached
    // only a `title` on a non-interactive div, which is hover-only and so
    // unreachable by touch at this very viewport.
    //
    // The shipped assertion this inverts protected the layout, and inverting it
    // alone would have removed that protection: the reason wraps inside an
    // `overflow: hidden` ancestor, so it grows the hero VERTICALLY, which no
    // document-width check can see, and `toContainText` matches `textContent`
    // without requiring the element to be visible at all. The four checks below
    // replace what the inversion gave up.
    const reason = page.getByTestId('hero-combined-reason');
    await expect(reason).toContainText(LONG_HERO_WARNING);
    await expect(reason).toBeVisible();
    const reasonGeometry = await reason.evaluate((node) => {
      const strip = node.closest('.hero-strip') as HTMLElement;
      const own = node.getBoundingClientRect();
      const outer = strip.getBoundingClientRect();
      return {
        clientWidth: node.clientWidth,
        scrollWidth: node.scrollWidth,
        clientHeight: node.clientHeight,
        scrollHeight: node.scrollHeight,
        containedX: own.left >= outer.left - 1 && own.right <= outer.right + 1,
        containedY: own.top >= outer.top - 1 && own.bottom <= outer.bottom + 1,
        stripHeight: outer.height,
      };
    });
    expect(reasonGeometry.scrollWidth).toBeLessThanOrEqual(reasonGeometry.clientWidth + 1);
    expect(reasonGeometry.scrollHeight).toBeLessThanOrEqual(reasonGeometry.clientHeight + 1);
    expect(reasonGeometry.containedX).toBe(true);
    expect(reasonGeometry.containedY).toBe(true);
    // A ceiling on the withheld hero itself. The reason is the tallest thing
    // this state adds, and the hero is the page's first card — a sentence that
    // pushes the whole board below the fold is a regression no width check can
    // report. Half the viewport is deliberately generous against the ~280px
    // this state measures at 390x844: it is a blow-up guard for a sentence that
    // stops wrapping or a zone that stops bounding it, not a pixel budget that
    // a font-metric change should redden.
    expect(reasonGeometry.stripHeight).toBeLessThanOrEqual(viewport.height * 0.5);
    await assertPageGeometry(page);
    await page.screenshot({ path: screenshotPath(viewport, 'all-bounded-warning'), fullPage: true });
  });
}
