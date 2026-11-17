// #923 S2 — a long same-name project label truncates its BASE name, never the
// `(n)` / qualifier suffix that tells two projects apart, and on the All tab it
// keeps its `Claude · ` / `Codex · ` source prefix (spec
// docs/superpowers/specs/2026-11-10-923-s2-projects-label-suffix-design.md §5).
//
// One in-page measurement (`measureLabel`) serves every case. It reads the
// cell's text nodes in document order — skipping a `.source-chip`, measured on
// its own — so it selects the same characters from the pre-change markup (one
// text node) and from the split markup (prefix, base and suffix spans). Each
// character's one-character Range rectangles are tested against the
// intersection of every clipping ancestor's padding box, per axis, up to the
// document, and the viewport. A character is visible when it has a positive
// rectangle and every rectangle lies inside that clip box within 0.5px, the
// tolerance for fractional layout coordinates only.
//
// The boundary between base and suffix is a literal of each case, so this file
// imports nothing from `src/` and runs unchanged against the baseline bundle.
import { mkdirSync } from 'node:fs';
import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test';
import { e2eRuntimePath, fulfilJson } from './utils';

const PAIR = [
  { label: 'customer-analytics-pipeline-service (1)', suffix: ' (1)' },
  { label: 'customer-analytics-pipeline-service (2)', suffix: ' (2)' },
] as const;
const QUALIFIED = 'customer-analytics-pipeline-service (client-engagements-archive-2026)';
const QUALIFIER = ' (client-engagements-archive-2026)';
const AMBIGUOUS = 'app (customer-alpha (b) (1)';
// A pair base long enough to widen an auto-layout table past a 390px modal
// (complete-diff review F1: past ~48 characters the suffix left the screen).
const LONG_BASE = 'customer-analytics-pipeline-service-backfill-orchestrator';
// A qualifier whose first token has no break opportunity and outgrows the line
// (review F2: the separator space was left alone on a line of its own).
const TOKEN_QUALIFIER = ' (my_company_internal_projects_archive)';
const TOKEN_QUALIFIED = `api${TOKEN_QUALIFIER}`;
// Wider than the Codex project-detail badge's available width at both
// viewports (at least 160 characters, spec §5.3).
const DETAIL_BASE = `${'customer-analytics-pipeline-service-'.repeat(5)}archive`;
const DETAIL_LABEL = `${DETAIL_BASE} (1)`;

const DESKTOP = { width: 1440, height: 900 } as const;
const PHONE = { width: 390, height: 844 } as const;
type Viewport = typeof DESKTOP | typeof PHONE;
type Source = 'claude' | 'codex' | 'all';

const PREFIX = { claude: 'Claude · ', codex: 'Codex · ' } as const;
const TOLERANCE = 0.5;

interface Rect { left: number; top: number; right: number; bottom: number; width: number; height: number }
interface Glyph { ch: string; rects: Rect[]; visible: boolean }
interface LabelMeasure {
  text: string;
  prefix: Glyph[];
  base: Glyph[];
  suffix: Glyph[];
  chip: Glyph[];
  chipText: string;
  baseClipTextOverflow: string | null;
  ellipsisOnBaseClip: boolean;
}

async function measureLabel(
  cell: Locator,
  opts: { expected: string; prefixLength: number; suffixLength: number },
): Promise<LabelMeasure> {
  return cell.evaluate((el, { prefixLength, suffixLength, tolerance }) => {
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    // Measure at rest horizontally: scrollIntoView may scroll an x-scrollable
    // ancestor (the modal body) sideways, which would bring a label that
    // overflows it into view and hide the overflow from the oracle.
    for (let a: Element | null = el.parentElement; a; a = a.parentElement) {
      if (a.scrollLeft) a.scrollLeft = 0;
    }
    const labelNodes: Text[] = [];
    const chipNodes: Text[] = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const chip = node.parentElement?.closest('.source-chip');
      if (chip && el.contains(chip)) chipNodes.push(node as Text);
      else labelNodes.push(node as Text);
    }
    // An inline box never clips, whatever its computed overflow says.
    const clips = (element: Element, axis: 'x' | 'y') => {
      const style = getComputedStyle(element);
      if (style.display === 'inline' || style.display === 'contents') return false;
      return (axis === 'x' ? style.overflowX : style.overflowY) !== 'visible';
    };
    // The ancestors whose overflow can clip `node`, nearest first. Overflow
    // clips a box only through its containing-block chain: a fixed box (the
    // modal roots) escapes every ancestor below the viewport, an absolute one
    // every non-positioned ancestor, unless a transform, filter, perspective or
    // containment makes the ancestor its containing block. The root element's
    // overflow belongs to the viewport, intersected separately. Measured on the
    // runner: with the page scrolled at 390px and the scroll lock's
    // `overflow: hidden` on <html> and <body>, a parent-chain walk shifted the
    // clip band by the scroll offset and called every glyph of a fixed modal
    // hidden while the screenshot showed them.
    const clippers = (node: Text) => {
      const out: Element[] = [];
      let escape: 'fixed' | 'absolute' | null = null;
      for (let a: Element | null = node.parentElement; a && a !== document.documentElement; a = a.parentElement) {
        const style = getComputedStyle(a);
        const containsAll = style.transform !== 'none' || style.perspective !== 'none'
          || style.filter !== 'none' || /paint|layout|strict|content/.test(style.contain);
        const containing = escape === 'fixed' ? containsAll
          : escape === 'absolute' ? style.position !== 'static' || containsAll
            : true;
        if (!containing) continue;
        out.push(a);
        escape = style.position === 'fixed' ? 'fixed' : style.position === 'absolute' ? 'absolute' : null;
      }
      return out;
    };
    const clipBox = (node: Text) => {
      let x0 = 0;
      let x1 = window.innerWidth;
      let y0 = 0;
      let y1 = window.innerHeight;
      for (const a of clippers(node)) {
        const r = a.getBoundingClientRect();
        if (clips(a, 'x')) {
          const left = r.left + a.clientLeft;
          x0 = Math.max(x0, left);
          x1 = Math.min(x1, left + a.clientWidth);
        }
        if (clips(a, 'y')) {
          const top = r.top + a.clientTop;
          y0 = Math.max(y0, top);
          y1 = Math.min(y1, top + a.clientHeight);
        }
      }
      return { x0, x1, y0, y1 };
    };
    const glyphsOf = (nodes: Text[]) => {
      const out: { ch: string; rects: { left: number; top: number; right: number; bottom: number; width: number; height: number }[]; visible: boolean; node: Text }[] = [];
      for (const node of nodes) {
        const box = clipBox(node);
        for (let i = 0; i < node.data.length; i += 1) {
          const range = document.createRange();
          range.setStart(node, i);
          range.setEnd(node, i + 1);
          const rects = Array.from(range.getClientRects()).map((r) => ({
            left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height,
          }));
          const ch = node.data[i];
          const visible = /\S/.test(ch)
            && rects.some((r) => r.width > 0 && r.height > 0)
            && rects.every((r) => r.left >= box.x0 - tolerance && r.right <= box.x1 + tolerance
              && r.top >= box.y0 - tolerance && r.bottom <= box.y1 + tolerance);
          out.push({ ch, rects, visible, node });
        }
      }
      return out;
    };
    const label = glyphsOf(labelNodes);
    const chip = glyphsOf(chipNodes);
    const base = label.slice(prefixLength, label.length - suffixLength);
    // (f) — the nearest ancestor of the base's text node that clips on x.
    let baseClipTextOverflow: string | null = null;
    const baseNode = base[0]?.node;
    const baseClipper = baseNode ? clippers(baseNode).find((a) => clips(a, 'x')) : undefined;
    if (baseClipper) baseClipTextOverflow = getComputedStyle(baseClipper).textOverflow;
    const strip = (glyphs: typeof label) => glyphs.map(({ ch, rects, visible }) => ({ ch, rects, visible }));
    return {
      text: label.map((g) => g.ch).join(''),
      prefix: strip(label.slice(0, prefixLength)),
      base: strip(base),
      suffix: strip(label.slice(label.length - suffixLength)),
      chip: strip(chip),
      chipText: chip.map((g) => g.ch).join(''),
      baseClipTextOverflow,
      ellipsisOnBaseClip: baseClipTextOverflow === 'ellipsis',
    };
  }, { prefixLength: opts.prefixLength, suffixLength: opts.suffixLength, tolerance: TOLERANCE });
}

const nonWs = (glyphs: Glyph[]) => glyphs.filter((g) => /\S/.test(g.ch));

function overlaps(a: Rect, b: Rect): boolean {
  const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return x > TOLERANCE && y > TOLERANCE;
}

interface Checks {
  a: boolean; b: boolean; c: boolean; d: boolean; e: boolean; f: boolean; chip: boolean;
  hiddenSuffix: string; hiddenPrefix: string; visibleBase: number; baseTotal: number;
  overlapping: string;
}

/** Assertions (a)–(f) of spec §5.2, as data, so a negative control can name the one it breaks. */
function checksOf(m: LabelMeasure): Checks {
  const suffix = nonWs(m.suffix);
  const prefix = nonWs(m.prefix);
  const base = nonWs(m.base);
  const visibleBase = base.filter((g) => g.visible);
  const others = [...m.prefix, ...m.suffix].flatMap((g) => g.rects);
  const overlapping = visibleBase.filter((g) => g.rects.some((r) => others.some((o) => overlaps(r, o))));
  return {
    a: suffix.length > 0 && suffix.every((g) => g.visible),
    b: prefix.every((g) => g.visible),
    c: base.some((g) => !g.visible),
    d: visibleBase.length >= 3,
    e: overlapping.length === 0,
    f: m.ellipsisOnBaseClip,
    chip: nonWs(m.chip).every((g) => g.visible),
    hiddenSuffix: suffix.filter((g) => !g.visible).map((g) => g.ch).join(''),
    hiddenPrefix: prefix.filter((g) => !g.visible).map((g) => g.ch).join(''),
    visibleBase: visibleBase.length,
    baseTotal: base.length,
    overlapping: overlapping.map((g) => g.ch).join(''),
  };
}

/** Soft-assert a protected label, so a failing run names every broken assertion. */
function expectProtected(
  name: string,
  m: LabelMeasure,
  expected: string,
  opts: { prefix: boolean; chip?: boolean; squeezed?: boolean },
): Checks {
  const squeezed = opts.squeezed ?? true;
  const c = checksOf(m);
  expect.soft(m.text, `${name}: label text`).toBe(expected);
  expect.soft(c.a, `${name}: (a) every suffix glyph visible; hidden "${c.hiddenSuffix}"`).toBe(true);
  if (opts.prefix) {
    expect.soft(c.b, `${name}: (b) every prefix glyph visible; hidden "${c.hiddenPrefix}"`).toBe(true);
  }
  if (squeezed) expect.soft(c.c, `${name}: (c) the squeeze is exercised (a base glyph is hidden)`).toBe(true);
  expect.soft(c.d, `${name}: (d) at least three base glyphs visible; got ${c.visibleBase}/${c.baseTotal}`).toBe(true);
  expect.soft(c.e, `${name}: (e) no visible base glyph overlaps the prefix or suffix; overlapping "${c.overlapping}"`).toBe(true);
  if (squeezed) expect.soft(c.f, `${name}: (f) the base truncates with an ellipsis; clip ancestor text-overflow ${m.baseClipTextOverflow}`).toBe(true);
  if (opts.chip) {
    expect.soft(m.chipText.trim().length > 0 && c.chip, `${name}: source chip "${m.chipText}" visible`).toBe(true);
  }
  return c;
}

async function attachMeasure(testInfo: TestInfo, name: string, m: LabelMeasure) {
  const summary = {
    text: m.text,
    baseClipTextOverflow: m.baseClipTextOverflow,
    checks: checksOf(m),
    glyphs: (['prefix', 'base', 'suffix', 'chip'] as const).map((part) => ({
      part,
      glyphs: m[part].map((g) => ({ ch: g.ch, visible: g.visible, rects: g.rects.map((r) => [r.left, r.top, r.width, r.height].map((v) => Math.round(v * 10) / 10)) })),
    })),
  };
  await testInfo.attach(`${name}-measure`, { body: JSON.stringify(summary, null, 1), contentType: 'application/json' });
}

/**
 * The gap from a table cell's label text to its source chip, on the chip's
 * line. Found in acceptance: a label that grows to fill the card's project cell
 * leaves its text where it was and pushes the chip to the cell's end, next to
 * the cost, instead of right after the name as before the change.
 */
async function chipGap(cell: Locator): Promise<number> {
  return cell.evaluate((el) => {
    const label = el.querySelector('.project-label');
    const chip = el.querySelector('.source-chip');
    if (!label || !chip) return Number.POSITIVE_INFINITY;
    const chipRect = chip.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(label);
    const rights = Array.from(range.getClientRects())
      .filter((r) => r.width > 0 && r.bottom > chipRect.top && r.top < chipRect.bottom)
      .map((r) => r.right);
    return rights.length ? chipRect.left - Math.max(...rights) : Number.POSITIVE_INFINITY;
  });
}

async function capture(locator: Locator, testInfo: TestInfo, name: string) {
  const dir = e2eRuntimePath('issue-923-s2-evidence');
  mkdirSync(dir, { recursive: true });
  const path = `${dir}/${name}.png`;
  await locator.screenshot({ path });
  await testInfo.attach(name, { path, contentType: 'image/png' });
}

async function selectSource(page: Page, source: Source) {
  const segment = page.locator(`.source-seg[data-source="${source}"]`);
  if (await segment.isVisible()) await segment.click();
  else await segment.evaluate((node: HTMLElement) => node.click());
  await expect(segment).toHaveClass(/is-active/);
}

async function ready(page: Page, viewport: Viewport) {
  await page.setViewportSize(viewport);
}

function panelRow(page: Page, text: string): Locator {
  return page.locator('#panel-projects .projects-row').filter({ hasText: text });
}

async function openProjectsModal(page: Page): Promise<Locator> {
  await page.getByRole('button', { name: 'Open Projects' }).click();
  const dialog = page.getByRole('dialog', { name: /Projects/ });
  await expect(dialog).toBeVisible();
  return dialog;
}

const tag = (viewport: Viewport) => `${viewport.width}x${viewport.height}`;

// A test-local envelope delivered through the real EventSource transport
// (the issues-877-873-layout.spec.ts pattern). With SharedWorker disabled and
// `/api/events` routed, every reconnect sees the same frame, so live fixture
// ticks cannot replace the labels under measurement.
async function serveMutated(page: Page, mutate: (envelope: any) => void) {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'SharedWorker', { configurable: true, value: undefined });
  });
  const response = await page.request.get('/api/data');
  expect(response.ok()).toBe(true);
  const envelope = await response.json() as any;
  mutate(envelope);
  envelope.data_version = 'issue-923-s2';
  await page.route('**/api/events', (route) => route.fulfill({
    contentType: 'text/event-stream',
    body: `event: update\ndata: ${JSON.stringify(envelope)}\n\n`,
  }));
  await page.goto('/');
}

/** The first Claude row takes `label` (and the legacy current-week key with it). */
function labelFirstClaudeRow(envelope: any, label: string) {
  const claudeRows = envelope.sources.claude.data.projects.aggregate.rows;
  expect(claudeRows.length).toBeGreaterThan(0);
  claudeRows[0].label = label;
  envelope.projects.current_week.rows[0].key = label;
}

async function serveEnvelope(page: Page) {
  await serveMutated(page, (envelope) => {
    labelFirstClaudeRow(envelope, QUALIFIED);
    // A non-drillable row and a sixth row, so the static explanation and the
    // `+N more` tail render beside the squeezed labels in the captures.
    envelope.sources.claude.data.projects.aggregate.rows.push(
      { key: 'project:issue-923-s2-static', label: 'static-project', source: 'claude', drillable: false, cost_usd: 0.004, sessions_count: 1 },
      { key: 'project:issue-923-s2-tail', label: 'tail-project', source: 'claude', drillable: true, cost_usd: 0.0001, sessions_count: 1 },
    );
    const codexRow = envelope.sources.codex.data.projects.rows.find((row: any) => row.label === 'project-red');
    expect(codexRow, 'the fixture Codex project-red row').toBeTruthy();
    codexRow.label = AMBIGUOUS;
  });
}

// ---- Panel (A3): the Codex pair at both widths, on Codex and All ----------

for (const viewport of [DESKTOP, PHONE]) {
  for (const source of ['codex', 'all'] as const) {
    test(`panel keeps the pair's suffix visible on ${source} at ${tag(viewport)}`, async ({ page }, testInfo) => {
      await ready(page, viewport);
      await page.goto('/');
      await selectSource(page, source);
      for (const { label } of PAIR) await expect(panelRow(page, label)).toHaveCount(1);
      await capture(page.locator('#panel-projects'), testInfo, `panel-${source}-${tag(viewport)}`);
      const prefix = source === 'all' ? PREFIX.codex : '';
      for (const { label, suffix } of PAIR) {
        const expected = prefix + label;
        const m = await measureLabel(panelRow(page, label).locator('.name'), {
          expected, prefixLength: prefix.length, suffixLength: suffix.length,
        });
        await attachMeasure(testInfo, label, m);
        expectProtected(`${source} ${tag(viewport)} "${label}"`, m, expected, { prefix: source === 'all' });
      }
    });
  }
}

// ---- Modal twins on Codex: ranked bars at 1440, table cards at 390 --------

test('Projects modal ranked bars keep the pair suffix at 1440x900 on codex', async ({ page }, testInfo) => {
  await ready(page, DESKTOP);
  await page.goto('/');
  await selectSource(page, 'codex');
  const dialog = await openProjectsModal(page);
  const rows = dialog.locator('[data-testid="projects-ranked-bars"] .projects-ranked-row');
  for (const { label } of PAIR) await expect(rows.filter({ hasText: label })).toHaveCount(1);
  await capture(dialog.locator('[data-testid="projects-ranked-bars"]'), testInfo, `modal-ranked-codex-${tag(DESKTOP)}`);
  for (const { label, suffix } of PAIR) {
    const m = await measureLabel(rows.filter({ hasText: label }).locator('.rk-label'), {
      expected: label, prefixLength: 0, suffixLength: suffix.length,
    });
    await attachMeasure(testInfo, label, m);
    expectProtected(`ranked "${label}"`, m, label, { prefix: false });
  }
});

test('Projects modal table keeps the pair suffix and chip at 390x844 on codex', async ({ page }, testInfo) => {
  await ready(page, PHONE);
  await page.goto('/');
  await selectSource(page, 'codex');
  const dialog = await openProjectsModal(page);
  const rows = dialog.getByTestId('projects-table-row');
  for (const { label } of PAIR) await expect(rows.filter({ hasText: label })).toHaveCount(1);
  await capture(dialog.locator('.projects-table'), testInfo, `modal-table-codex-${tag(PHONE)}`);
  for (const { label, suffix } of PAIR) {
    const m = await measureLabel(rows.filter({ hasText: label }).locator('td.project'), {
      expected: label, prefixLength: 0, suffixLength: suffix.length,
    });
    await attachMeasure(testInfo, label, m);
    // Measured on the baseline bundle: at 390px the Codex card's project cell
    // holds the whole pair label and its chip on one line, so this twin is
    // not squeezed and (c)/(f) do not apply. The squeezed table twin is the
    // Claude-qualifier case below.
    expectProtected(`table "${label}"`, m, label, { prefix: false, chip: true, squeezed: false });
  }
  // The chip stays right after the name (the cell's 6px gap), on a pair row and
  // on a short single-name row, where a growing label would leave a wide gap.
  for (const label of [...PAIR.map((p) => p.label), 'project-red']) {
    const gap = await chipGap(rows.filter({ hasText: label }).locator('td.project'));
    expect.soft(gap, `table "${label}": the source chip follows the label`).toBeLessThanOrEqual(8);
  }
});

test('Projects modal table keeps a long base\'s suffix and chip on screen at 390x844 on codex', async ({ page }, testInfo) => {
  await ready(page, PHONE);
  await serveMutated(page, (envelope) => {
    let n = 0;
    for (const row of envelope.sources.codex.data.projects.rows) {
      if (typeof row.label === 'string' && row.label.startsWith('customer-analytics-pipeline-service (')) {
        n += 1;
        row.label = `${LONG_BASE} (${n})`;
      }
    }
    expect(n, 'the fixture Codex pair').toBe(2);
  });
  await selectSource(page, 'codex');
  const dialog = await openProjectsModal(page);
  const rows = dialog.getByTestId('projects-table-row');
  const labels = [`${LONG_BASE} (1)`, `${LONG_BASE} (2)`];
  for (const label of labels) await expect(rows.filter({ hasText: label })).toHaveCount(1);
  const body = dialog.locator('.modal-body').first();
  await body.evaluate((node) => { node.scrollLeft = 0; });
  await capture(dialog.locator('.projects-table'), testInfo, `modal-table-codex-long-${tag(PHONE)}`);
  // The card table never grows past the modal: an auto-layout table took the
  // base's unbreakable width as its minimum and pushed suffix and chip off it.
  const overflow = await body.evaluate((node) => node.scrollWidth - node.clientWidth);
  expect.soft(overflow, 'the modal body does not scroll sideways').toBeLessThanOrEqual(0);
  for (const label of labels) {
    const m = await measureLabel(rows.filter({ hasText: label }).locator('td.project'), {
      expected: label, prefixLength: 0, suffixLength: ' (1)'.length,
    });
    await attachMeasure(testInfo, label, m);
    expectProtected(`table long "${label}"`, m, label, { prefix: false, chip: true });
  }
});

// ---- Detail twin: the Codex project-detail header badge --------------------

for (const viewport of [PHONE, DESKTOP]) {
  test(`Codex project detail badge keeps the suffix at ${tag(viewport)}`, async ({ page }, testInfo) => {
    await ready(page, viewport);
    await page.route('**/api/source/*/project/*', async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.data.label = DETAIL_LABEL;
      await fulfilJson(route, response, body);
    });
    await page.goto('/');
    await selectSource(page, 'codex');
    const row = panelRow(page, PAIR[0].label);
    await expect(row).toHaveCount(1);
    await row.click();
    const detail = page.getByRole('dialog', { name: 'Project detail' });
    await expect(detail).toBeVisible();
    const badge = detail.locator('[data-testid="codex-project-detail"] .sd-project-label');
    await expect(badge).toHaveText(DETAIL_LABEL);
    await capture(detail.locator('[data-testid="codex-project-detail"] .m-chipstrip'), testInfo, `detail-codex-${tag(viewport)}`);
    const m = await measureLabel(badge, { expected: DETAIL_LABEL, prefixLength: 0, suffixLength: ' (1)'.length });
    await attachMeasure(testInfo, 'detail', m);
    expectProtected(`detail ${tag(viewport)}`, m, DETAIL_LABEL, { prefix: false });
  });
}

// ---- Claude-qualifier squeeze (test-local envelope) -------------------------

function expectWrapped(name: string, m: LabelMeasure) {
  const tops = new Set(nonWs(m.suffix).flatMap((g) => g.rects.map((r) => Math.round(r.top))));
  expect.soft(tops.size, `${name}: the qualifier wraps onto more than one line`).toBeGreaterThan(1);
}

test('Claude qualifier stays visible in the All panel at 1440x900', async ({ page }, testInfo) => {
  await ready(page, DESKTOP);
  await serveEnvelope(page);
  await selectSource(page, 'all');
  await expect(page.locator('#panel-projects .panel-withheld')).toHaveCount(0);
  const row = panelRow(page, QUALIFIED);
  await expect(row).toHaveCount(1);
  await expect(row.locator('.cost')).toBeVisible();
  await expect(row.locator('.pct')).toBeVisible();
  await expect(page.locator('#panel-projects .projects-row-nodrill')).toBeVisible();
  await expect(page.locator('#panel-projects .projects-row.tail')).toBeVisible();
  await capture(page.locator('#panel-projects'), testInfo, `panel-all-claude-qualifier-${tag(DESKTOP)}`);
  // The desktop card body scrolls at a fixed row height, so the lower rows are
  // captured scrolled into view: the squeezed row with its cost and share, then
  // the static explanation and the tail.
  await row.scrollIntoViewIfNeeded();
  for (const part of [row.locator('.cost'), row.locator('.pct')]) await expect(part).toBeInViewport();
  await capture(page.locator('#panel-projects'), testInfo, `panel-all-claude-qualifier-row-${tag(DESKTOP)}`);
  await page.locator('#panel-projects .projects-row.tail').scrollIntoViewIfNeeded();
  await expect(page.locator('#panel-projects .projects-row-nodrill')).toBeInViewport();
  await expect(page.locator('#panel-projects .projects-row.tail')).toBeInViewport();
  await capture(page.locator('#panel-projects'), testInfo, `panel-all-claude-qualifier-tail-${tag(DESKTOP)}`);
  const expected = PREFIX.claude + QUALIFIED;
  const m = await measureLabel(row.locator('.name'), {
    expected, prefixLength: PREFIX.claude.length, suffixLength: QUALIFIER.length,
  });
  await attachMeasure(testInfo, 'claude-qualifier', m);
  expectProtected('all claude qualifier', m, expected, { prefix: true });
  expectWrapped('all claude qualifier', m);
});

test('Claude qualifier stays visible in the Claude panel at 1440x900', async ({ page }, testInfo) => {
  await ready(page, DESKTOP);
  await serveEnvelope(page);
  await selectSource(page, 'claude');
  const row = panelRow(page, QUALIFIED);
  await expect(row).toHaveCount(1);
  await capture(page.locator('#panel-projects'), testInfo, `panel-claude-claude-qualifier-${tag(DESKTOP)}`);
  const m = await measureLabel(row.locator('.name'), {
    expected: QUALIFIED, prefixLength: 0, suffixLength: QUALIFIER.length,
  });
  await attachMeasure(testInfo, 'claude-qualifier', m);
  expectProtected('claude claude qualifier', m, QUALIFIED, { prefix: false });
  expectWrapped('claude claude qualifier', m);
});

test('Claude qualifier stays visible in the All modal ranked bars at 1440x900', async ({ page }, testInfo) => {
  await ready(page, DESKTOP);
  await serveEnvelope(page);
  await selectSource(page, 'all');
  await expect(panelRow(page, QUALIFIED)).toHaveCount(1);
  const dialog = await openProjectsModal(page);
  const row = dialog.locator('[data-testid="projects-ranked-bars"] .projects-ranked-row').filter({ hasText: QUALIFIED });
  await expect(row).toHaveCount(1);
  await capture(dialog.locator('[data-testid="projects-ranked-bars"]'), testInfo, `modal-ranked-all-claude-qualifier-${tag(DESKTOP)}`);
  const m = await measureLabel(row.locator('.rk-label'), {
    expected: QUALIFIED, prefixLength: 0, suffixLength: QUALIFIER.length,
  });
  await attachMeasure(testInfo, 'claude-qualifier', m);
  expectProtected('ranked claude qualifier', m, QUALIFIED, { prefix: false });
});

test('Claude qualifier stays visible in the All modal table at 390x844', async ({ page }, testInfo) => {
  await ready(page, PHONE);
  await serveEnvelope(page);
  await selectSource(page, 'all');
  await expect(panelRow(page, QUALIFIED)).toHaveCount(1);
  const dialog = await openProjectsModal(page);
  const row = dialog.getByTestId('projects-table-row').filter({ hasText: QUALIFIED });
  await expect(row).toHaveCount(1);
  await capture(dialog.locator('.projects-table'), testInfo, `modal-table-all-claude-qualifier-${tag(PHONE)}`);
  const m = await measureLabel(row.locator('td.project'), {
    expected: QUALIFIED, prefixLength: 0, suffixLength: QUALIFIER.length,
  });
  await attachMeasure(testInfo, 'claude-qualifier', m);
  expectProtected('table claude qualifier', m, QUALIFIED, { prefix: false, chip: true });
});

/** Suffix lines (by glyph top) that hold only whitespace: a separator stranded on its own line. */
function blankSuffixLines(m: LabelMeasure): number {
  const lines = new Map<number, string>();
  for (const g of m.suffix) {
    for (const r of g.rects) {
      if (r.width <= 0) continue;
      const top = Math.round(r.top);
      lines.set(top, (lines.get(top) ?? '') + g.ch);
    }
  }
  return [...lines.values()].filter((text) => !/\S/.test(text)).length;
}

test('a qualifier with an unbreakable first token wraps without a blank line at 1440x900', async ({ page }, testInfo) => {
  await ready(page, DESKTOP);
  await serveMutated(page, (envelope) => labelFirstClaudeRow(envelope, TOKEN_QUALIFIED));
  await selectSource(page, 'all');
  const row = panelRow(page, TOKEN_QUALIFIED);
  await expect(row).toHaveCount(1);
  await row.scrollIntoViewIfNeeded();
  await capture(page.locator('#panel-projects'), testInfo, `panel-all-token-qualifier-${tag(DESKTOP)}`);
  const expected = PREFIX.claude + TOKEN_QUALIFIED;
  const panel = await measureLabel(row.locator('.name'), {
    expected, prefixLength: PREFIX.claude.length, suffixLength: TOKEN_QUALIFIER.length,
  });
  await attachMeasure(testInfo, 'token-qualifier-panel', panel);
  expect.soft(panel.text, 'token qualifier panel: label text').toBe(expected);
  expect.soft(checksOf(panel).a, 'token qualifier panel: (a) every suffix glyph visible').toBe(true);
  expect.soft(blankSuffixLines(panel), 'token qualifier panel: no whitespace-only suffix line').toBe(0);
  const dialog = await openProjectsModal(page);
  const ranked = dialog.locator('[data-testid="projects-ranked-bars"] .projects-ranked-row').filter({ hasText: TOKEN_QUALIFIED });
  await expect(ranked).toHaveCount(1);
  await capture(dialog.locator('[data-testid="projects-ranked-bars"]'), testInfo, `modal-ranked-all-token-qualifier-${tag(DESKTOP)}`);
  const bars = await measureLabel(ranked.locator('.rk-label'), {
    expected: TOKEN_QUALIFIED, prefixLength: 0, suffixLength: TOKEN_QUALIFIER.length,
  });
  await attachMeasure(testInfo, 'token-qualifier-ranked', bars);
  expect.soft(checksOf(bars).a, 'token qualifier ranked: (a) every suffix glyph visible').toBe(true);
  expect.soft(blankSuffixLines(bars), 'token qualifier ranked: no whitespace-only suffix line').toBe(0);
});

// ---- Ambiguous label: shown whole, never truncated ------------------------

test('an unbalanced Codex label renders every character on All at 1440x900', async ({ page }, testInfo) => {
  await ready(page, DESKTOP);
  await serveEnvelope(page);
  await selectSource(page, 'all');
  const row = panelRow(page, AMBIGUOUS);
  await expect(row).toHaveCount(1);
  await capture(page.locator('#panel-projects'), testInfo, `panel-all-ambiguous-${tag(DESKTOP)}`);
  const expected = PREFIX.codex + AMBIGUOUS;
  const m = await measureLabel(row.locator('.name'), {
    expected, prefixLength: PREFIX.codex.length, suffixLength: 0,
  });
  await attachMeasure(testInfo, 'ambiguous', m);
  expect.soft(m.text, 'ambiguous: label text').toBe(expected);
  const hidden = nonWs([...m.prefix, ...m.base]).filter((g) => !g.visible).map((g) => g.ch).join('');
  expect.soft(hidden, 'ambiguous: every non-whitespace glyph visible').toBe('');
  // The label starts beside its prefix rather than leaving `Codex ·` alone on
  // the first line (review F8).
  const firstTop = (glyphs: Glyph[]) => Math.round(nonWs(glyphs)[0]?.rects[0]?.top ?? Number.NaN);
  expect.soft(firstTop(m.base), 'ambiguous: the label starts on the prefix line').toBe(firstTop(m.prefix));
});

// ---- Negative controls: the oracle detects both failure shapes ------------

const CONTROLS = [
  {
    name: 'overflowing base',
    css: '#panel-projects .projects-row .project-label .pl-base { overflow: visible !important; text-overflow: clip !important }',
    probe: '.pl-base',
    computed: { overflowX: 'visible' },
    breaks: 'e' as const,
  },
  {
    name: 'internally clipped suffix',
    // The qualifier span carries its own wrapping, so it is pinned to `pre` too.
    css: '#panel-projects .projects-row .project-label .pl-suffix { flex: none !important; max-width: 1ch !important; overflow: hidden !important; white-space: pre !important }'
      + ' #panel-projects .projects-row .project-label .pl-suffix .pl-qual { white-space: pre !important }',
    probe: '.pl-suffix',
    computed: { overflowX: 'hidden', whiteSpace: 'pre' },
    breaks: 'a' as const,
  },
];

test('negative controls: the oracle fails an overflowing base and a clipped suffix at 1440x900 on codex', async ({ page }, testInfo) => {
  await ready(page, DESKTOP);
  await page.goto('/');
  await selectSource(page, 'codex');
  const { label, suffix } = PAIR[0];
  const cell = panelRow(page, label).locator('.name');
  await expect(cell).toHaveCount(1);
  const measure = () => measureLabel(cell, { expected: label, prefixLength: 0, suffixLength: suffix.length });
  const positive = (name: string, m: LabelMeasure) => {
    const c = checksOf(m);
    expect(m.text, `${name}: label text`).toBe(label);
    expect({ a: c.a, c: c.c, d: c.d, e: c.e, f: c.f }, `${name}: positive result`).toEqual({ a: true, c: true, d: true, e: true, f: true });
  };
  positive('before the controls', await measure());
  for (const control of CONTROLS) {
    await page.evaluate((css) => {
      const style = document.createElement('style');
      style.id = 'issue-923-s2-control';
      style.textContent = css;
      document.head.appendChild(style);
    }, control.css);
    const computed = await cell.locator(control.probe).evaluate((node) => {
      const style = getComputedStyle(node);
      return { overflowX: style.overflowX, whiteSpace: style.whiteSpace };
    });
    for (const [key, value] of Object.entries(control.computed)) {
      expect(computed[key as keyof typeof computed], `${control.name}: computed ${key} wins the cascade`).toBe(value);
    }
    const broken = await measure();
    await attachMeasure(testInfo, `control-${control.breaks}`, broken);
    expect(checksOf(broken)[control.breaks], `${control.name}: assertion (${control.breaks}) fails`).toBe(false);
    await page.evaluate(() => document.getElementById('issue-923-s2-control')?.remove());
    positive(`after removing ${control.name}`, await measure());
  }
});
