import { test, expect, type Page } from '@playwright/test';

async function openSessionD(page: Page) {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole(
    'button', { name: 'Codex', exact: true },
  ).click();
  await page.locator('.conv-rail-row').filter({
    hasText: 'User-authored ::git-stage',
  }).click();
  await expect(page.locator('.conv-reader-body')).toBeVisible();
}

for (const viewport of [
  { label: 'desktop', width: 1440, height: 900 },
  { label: 'mobile', width: 390, height: 844 },
]) {
  test(`#499 — find reaches an injected-context body on ${viewport.label}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openSessionD(page);
    await page.getByRole('button', { name: 'Find in conversation' }).click();
    await page.locator('.conv-findbar-input').fill('Synthetic agent instructions');
    await expect(page.locator('.conv-findbar-count')).toHaveText('1 / 1 matches');

    const context = page.locator('details.conv-meta--context');
    await expect(context).toHaveCount(1);
    await expect(context).toHaveAttribute('open', '');
    const current = context.locator('mark[data-find-current="true"]');
    await expect(current).toHaveCount(1);
    await expect(current).toHaveText('Synthetic agent instructions');
    await expect(current).toBeVisible();
  });
}

// #463 S5 (F18, #493) — bring the injected-context meta row into the reader.
// It is the only kind that renders BOTH a `.conv-meta-label` ("SESSION CONTEXT")
// and a `.conv-meta-name` ("· agents, environment"), which are the two elements
// #493 reports shattering; the reader is virtualized, so the row has to be
// scrolled to rather than assumed present. e2e/serve.sh appends it to the
// runtime copy of the Session D rollout.
// The row is the LAST item of the conversation, and the reader is virtualized,
// so it is not mounted on open. Find is NOT the mechanism: the conversation's
// find index reports `0 / 0 matches` for text inside an injected-context meta
// body, so a find-driven reveal would silently do nothing and leave this test
// measuring only the notification rows it was already measuring. Scroll the
// reader to its end instead, polling because mounting the tail can itself change
// the scroll height.
// The scroll, the presence check and the MEASUREMENT are one page evaluation,
// and the whole evaluation is what the poll retries. Anything that checks the row
// and then measures it in a later call has a window in between: mounting the tail
// changes the reader's scroll height, so a row that has just appeared can be
// unmounted a frame later, and nothing after the poll re-drives the scroll to
// bring it back. Both recorded failures are that window — at 430 px the check saw
// one `.conv-meta-label` and zero `.conv-meta-name` while a direct probe of the
// same page showed a single row carrying both, and at 390 px a `toHaveCount(1)`
// that only auto-retries timed out against a row that had gone. Measuring inside
// the evaluation closes the window rather than narrowing it: JavaScript does not
// yield between the scroll and the reads, so React cannot re-render mid-capture,
// and an incomplete capture is simply discarded and re-driven.
interface MetaMetric {
  text: string;
  lines: number;
  lineHeight: number;
  height: number;
  scrollWidth: number;
  clientWidth: number;
}
interface MetaCapture {
  context: { label: string; name: string };
  labels: MetaMetric[];
}

async function measureMetaLabels(page: Page): Promise<MetaCapture> {
  await expect(page.locator('.conv-reader-body')).toBeVisible();
  let capture: MetaCapture = { context: { label: '', name: '' }, labels: [] };
  await expect
    .poll(async () => {
      capture = await page.evaluate(() => {
        const empty = { context: { label: '', name: '' }, labels: [] };
        const reader = document.querySelector('.conv-reader-body');
        if (!reader) return empty;
        reader.scrollTop = reader.scrollHeight;
        // Report nothing until the injected-context row is mounted with BOTH of
        // its elements. Without this the always-mounted notification rows would
        // satisfy a "more than zero" exit on the first iteration and the poll
        // would stop before ever reaching the row #493 is about.
        const rows = document.querySelectorAll('.conv-meta--context');
        if (rows.length !== 1) return empty;
        const label = rows[0].querySelectorAll('.conv-meta-label');
        const name = rows[0].querySelectorAll('.conv-meta-name');
        if (label.length !== 1 || name.length !== 1) return empty;
        const labels = Array.from(document.querySelectorAll(
          '.conv-meta > summary .conv-meta-label, .conv-meta > summary .conv-meta-name',
        )).map((node) => {
          const el = node as HTMLElement;
          const cs = getComputedStyle(el);
          const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2;
          return {
            text: (el.textContent ?? '').trim(),
            lines: Math.round(el.getBoundingClientRect().height / lh),
            lineHeight: lh,
            height: el.getBoundingClientRect().height,
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
          };
        });
        return {
          context: {
            label: (label[0].textContent ?? '').trim(),
            name: (name[0].textContent ?? '').trim(),
          },
          labels,
        };
      });
      return capture.labels.length;
    }, { timeout: 20_000, message: 'the injected-context meta row never mounted with both its elements' })
    .toBeGreaterThan(0);
  return capture;
}

for (const width of [390, 430]) {
  test(`#463 S5 — meta labels wrap at whitespace, never mid-word (${width}px)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await openSessionD(page);
    const { context, labels } = await measureMetaLabels(page);

    // A bare "more than zero" guard is not enough. The notification rows are
    // always mounted, so a measured set made only of those would pass while never
    // observing the row #493 is about. Name the two elements that have to be in
    // the set, read from the row itself rather than hardcoded, so the guard cannot
    // drift from the fixture. This also pins the `> summary` selector: if a label
    // stopped being rendered inside the summary row, the set would no longer
    // contain it.
    const texts = labels.map((metric) => metric.text);
    expect(texts.length, 'no meta labels rendered — the fixture cannot observe this').toBeGreaterThan(0);
    expect([context.label, context.name], 'an empty needle would make the guard below vacuous').not.toContain('');
    expect(texts, 'the injected-context label was not among the measured elements').toContain(context.label);
    expect(texts, 'the injected-context sections name was not among the measured elements').toContain(context.name);

    for (const { text, lines, lineHeight, height, scrollWidth, clientWidth } of labels) {
      // A whitespace-wrapped label needs at most one line per whitespace-separated
      // word. More lines than words means a word was broken mid-token.
      //
      // BOTH assertions are soft. Playwright aborts a test at the first failing
      // hard `expect`, and the notification rows precede the appended context row
      // in DOM order — so a recorded RED run failed on "overflows its box" for two
      // notification labels and NEVER REACHED the lines-vs-words assertion, which
      // is the one that actually observes the mid-word shatter. One failing
      // element must not mask another element's different failure.
      const words = text.split(/\s+/).filter(Boolean).length || 1;
      expect.soft(lines, `"${text}" rendered on ${lines} lines for ${words} words (h=${height}, lh=${lineHeight})`)
        .toBeLessThanOrEqual(words);
      expect.soft(scrollWidth, `"${text}" overflows its box`).toBeLessThanOrEqual(clientWidth + 1);
    }

    const doc = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(doc.scrollWidth, 'page scrolls horizontally').toBeLessThanOrEqual(doc.clientWidth);
  });
}

// #463 S5 (F24d, spec §4.2 and §4.8) — the observable claim of the deep-link
// work: the rail lands on the target's source WITH the target row marked
// current, even though a rail search was active when the link was followed.
// Nothing at any other layer asserts a rendered current row — the unit tests see
// dispatched actions and pushed history entries, not the rail — so without this
// case the session's headline claim rests entirely on manual QA.
test('#463 S5 — a cross-source permalink lands on the right tab with the row current', async ({ page }) => {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  const target = page.locator('.conv-rail-row').filter({ hasText: 'User-authored ::git-stage' });
  await target.click();
  await expect(page.locator('.conv-reader-body')).toBeVisible();
  const permalink = await page.evaluate(() => window.location.hash);
  expect(permalink, 'the reader did not write a qualified Codex hash').toContain('/source/codex/');

  // Leave for the Claude tab and put an active needle in the rail. The needle
  // matches a Claude conversation, so it is genuinely filtering, and it cannot
  // match the Codex target — which is the state that used to leave a followed
  // permalink with no current row.
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Claude', exact: true }).click();
  const needle = page.locator('.conv-rail-search-input');
  await needle.fill('thinking');
  await expect(page.locator('.conv-rail-row')).not.toHaveCount(0);
  await expect(page.locator('.conv-rail-source-btn.is-on')).toHaveText('Claude');

  // Follow the permalink the way a pasted link does inside a live tab.
  await page.evaluate((hash) => { window.location.hash = hash; }, permalink);

  await expect(page.locator('.conv-rail-source-btn.is-on')).toHaveText('Codex');
  await expect(needle).toHaveValue('');
  const current = page.locator('.conv-rail-row.is-active');
  await expect(current).toHaveCount(1);
  await expect(current).toContainText('User-authored ::git-stage');
});

// #463 S5 (review F5) — the other direction, and the reason this has to be a
// browser assertion. Chromium announces a fresh `location.hash` assignment with a
// popstate before its hashchange, exactly as it announces a Back, so "a popstate
// arrived" cannot mean "this is a Back". The router therefore stamps the entries
// it writes and reads the traversal from the stamp — and only a real browser
// produces the events and the stamps to check that against.
test('#463 S5 — Back keeps the rail search a fresh link would clear', async ({ page }) => {
  await page.goto('/#/conversations');
  await page.locator('.conv-rail-source').getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.conv-rail-row').filter({ hasText: 'User-authored ::git-stage' }).click();
  await expect(page.locator('.conv-reader-body')).toBeVisible();
  const first = await page.evaluate(() => window.location.hash);

  await page.locator('.conv-rail-row').filter({ hasText: 'Session E visible prompt A' }).click();
  await expect.poll(() => page.evaluate(() => window.location.hash)).not.toBe(first);

  const needle = page.locator('.conv-rail-search-input');
  await needle.fill('git');
  await expect(needle).toHaveValue('git');

  await page.goBack();
  await expect.poll(() => page.evaluate(() => window.location.hash)).toBe(first);
  await expect(needle).toHaveValue('git');
});

test.describe('Session E compact reader polish', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('keeps provider labels intact and the complete Codex token strip unclipped', async ({ page }) => {
    await openSessionD(page);
    const lifecycle = page.locator('.conv-meta--notification').filter({
      hasText: 'Errored lifecycle answer.',
    });
    await expect(lifecycle).toHaveCount(1);
    const label = lifecycle.locator('.conv-meta-label');
    // #463 S5 — #335's `nowrap` + ellipsis + `title` treatment is deliberately
    // gone. A `title` on a non-focusable span is not invokable by a sighted
    // touch user, so it replaced shattered text with truncation nobody can
    // read. The label now wraps at whitespace instead, and the property that
    // matters is that the WHOLE label is displayed and nothing is clipped.
    await expect(label).toHaveText('Codex task complete');
    await expect(label).not.toHaveAttribute('title', /./);
    const labelMetrics = await label.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        overflowWrap: style.overflowWrap,
        wordBreak: style.wordBreak,
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
      };
    });
    expect(labelMetrics.overflowWrap).toBe('break-word');
    expect(labelMetrics.wordBreak).toBe('normal');
    expect(labelMetrics.scrollWidth).toBeLessThanOrEqual(labelMetrics.clientWidth + 1);

    const stripMetrics = await page.locator('.conv-provider-strip').evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
    }));
    expect(stripMetrics.clientHeight).toBeGreaterThanOrEqual(stripMetrics.scrollHeight);
    const tokenStrip = page.locator('.conv-provider-tokens');
    await expect(tokenStrip).toContainText('in 0');
    await expect(tokenStrip).toContainText('out 0');
    await expect(tokenStrip).toContainText('cached in 0');
    await expect(tokenStrip).toContainText('reasoning out 0');
    expect(await tokenStrip.evaluate((element) => getComputedStyle(element).whiteSpace)).toBe('normal');
    expect(await page.evaluate(
      () => document.documentElement.scrollWidth === document.documentElement.clientWidth,
    )).toBe(true);
  });

  test('keeps retained native families silent while long provider token fields wrap', async ({ page }) => {
    await page.goto('/#/conversations');
    await page.locator('.conv-rail-source').getByRole(
      'button', { name: 'Codex', exact: true },
    ).click();
    const sessionE = page.locator('.conv-rail-row').filter({
      hasText: 'Session E visible prompt A',
    });
    await expect(sessionE).toHaveCount(1);
    await sessionE.click();

    const tokenStrip = page.locator('.conv-provider-tokens');
    await expect(tokenStrip).toContainText('in 123.5M');
    await expect(tokenStrip).toContainText('out 76.5M');
    await expect(tokenStrip).toContainText('cached in 98.8M');
    await expect(tokenStrip).toContainText('reasoning out 54.3M');
    const strip = page.locator('.conv-provider-strip');
    expect(await strip.evaluate((element) => element.clientHeight >= element.scrollHeight)).toBe(true);

    await expect(page.locator('.conv-reader-item[data-item-index]')).toHaveCount(4);
    const reader = page.locator('.conv-reader-body');
    await expect(reader).not.toContainText('SESSION_E_PRIVATE_INSTRUCTION_CANARY');
    await expect(reader).not.toContainText('/synthetic/private/session-e/workspace');
    await expect(reader).not.toContainText('native-secret-opaque-335');
    expect(await page.evaluate(
      () => document.documentElement.scrollWidth === document.documentElement.clientWidth,
    )).toBe(true);
  });
});
