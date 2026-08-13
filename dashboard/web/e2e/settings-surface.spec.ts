import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

// #513 S2 §6.4 — the nine items JSDOM cannot decide.
//
// What belongs here and nowhere else: `@media` state, real scrolling and
// scroll-lock, trusted pointer and keyboard events, computed focus rings, and
// geometry. The registry, the reducer, the issue routing and the manifest
// partition are all decided by the unit suites; repeating them here would cost
// a browser and prove nothing extra.
//
// Conventions follow the rest of `e2e/`: chromium only, serial, zero retries,
// condition-based waits and no fixed sleeps.

const SECTION_ORDER = [
  'display',
  'sessions',
  'alerts',
  'viewer',
  'access',
  'restore',
  'cli',
] as const;

async function openSettings(page: Page) {
  await page.goto('/');
  await page.locator('.dash-grid').waitFor();
  await page.keyboard.press('s');
  await page.locator('#settings-root').waitFor();
  await page.locator('#settings-scroller').waitFor();
}

/** The rail entry currently marked `aria-current="location"`. */
async function activeSection(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const active = document.querySelector('.settings-rail-link[aria-current="location"]');
    if (!active) return null;
    const index = Array.from(
      document.querySelectorAll('.settings-rail-link'),
    ).indexOf(active);
    const headings = Array.from(
      document.querySelectorAll('[data-settings-section]'),
    );
    return headings[index]?.getAttribute('data-settings-section') ?? null;
  });
}

test.describe('#513 S2 — the settings surface in a real browser', () => {
  test('the rail is a column at 1440 and a horizontal strip at 390', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openSettings(page);
    const wide = await page.evaluate(() => {
      const rail = document.querySelector('.settings-rail')!;
      const main = document.querySelector('.settings-main')!;
      const list = rail.querySelector('ul')!;
      return {
        mainDirection: getComputedStyle(main).flexDirection,
        listDisplay: getComputedStyle(list).display,
        railRight: rail.getBoundingClientRect().right,
        scrollerLeft: document.querySelector('#settings-scroller')!.getBoundingClientRect().left,
      };
    });
    expect(wide.mainDirection).toBe('row');
    expect(wide.listDisplay).not.toBe('flex');
    // A column: the rail ends at or before the scroller begins.
    expect(wide.railRight).toBeLessThanOrEqual(wide.scrollerLeft + 1);

    await page.setViewportSize({ width: 390, height: 844 });
    const narrow = await page.evaluate(() => {
      const rail = document.querySelector('.settings-rail')!;
      const main = document.querySelector('.settings-main')!;
      const list = rail.querySelector('ul')!;
      return {
        mainDirection: getComputedStyle(main).flexDirection,
        listDisplay: getComputedStyle(list).display,
        railBottom: rail.getBoundingClientRect().bottom,
        scrollerTop: document.querySelector('#settings-scroller')!.getBoundingClientRect().top,
      };
    });
    expect(narrow.mainDirection).toBe('column');
    expect(narrow.listDisplay).toBe('flex');
    // A strip: the rail ends above where the scroller begins.
    expect(narrow.railBottom).toBeLessThanOrEqual(narrow.scrollerTop + 1);
  });

  test('section navigation travels inside the content pane without scrolling the modal card', async ({ page }) => {
    for (const size of [
      { width: 1440, height: 900 },
      { width: 1440, height: 1600 },
      { width: 390, height: 844 },
    ]) {
      await page.setViewportSize(size);
      await page.emulateMedia({ reducedMotion: 'no-preference' });
      await openSettings(page);
      await page.evaluate(() => {
        const state = window as unknown as { __settingsScrollSamples: number[] };
        const scroller = document.querySelector<HTMLElement>('#settings-scroller')!;
        state.__settingsScrollSamples = [scroller.scrollTop];
        scroller.addEventListener('scroll', () => {
          state.__settingsScrollSamples.push(scroller.scrollTop);
        });
      });

      // A tall pane used to clamp this penultimate target to maximum scroll;
      // the scroll-spy then highlighted the final CLI section instead of the
      // Restore entry the user had selected (observed in Safari).
      await page.getByRole('button', { name: 'Restore defaults', exact: true }).click();
      await expect.poll(() => activeSection(page)).toBe('restore');
      await expect.poll(() => page.evaluate(() => {
        const scroller = document.querySelector<HTMLElement>('#settings-scroller')!;
        const heading = document.querySelector<HTMLElement>('[data-settings-section="restore"]')!;
        return Math.round(heading.getBoundingClientRect().top - scroller.getBoundingClientRect().top);
      })).toBe(16);

      await page.getByRole('button', { name: 'Managed from the CLI', exact: true }).click();
      await expect.poll(() => page.evaluate(() => {
        const scroller = document.querySelector<HTMLElement>('#settings-scroller')!;
        const heading = document.querySelector<HTMLElement>('[data-settings-section="cli"]')!;
        return Math.round(heading.getBoundingClientRect().top - scroller.getBoundingClientRect().top);
      })).toBe(16);
      await expect.poll(() => activeSection(page)).toBe('cli');

      const result = await page.evaluate(() => {
        const state = window as unknown as { __settingsScrollSamples: number[] };
        const card = document.querySelector<HTMLElement>('#settings-root .modal-card')!;
        const header = document.querySelector<HTMLElement>('#settings-root .modal-header')!;
        const scroller = document.querySelector<HTMLElement>('#settings-scroller')!;
        const heading = document.querySelector<HTMLElement>('[data-settings-section="cli"]')!;
        const cardRect = card.getBoundingClientRect();
        const headerRect = header.getBoundingClientRect();
        const finalTop = scroller.scrollTop;
        return {
          cardScrollTop: card.scrollTop,
          contentScrollTop: finalTop,
          headingGap: Math.round(
            heading.getBoundingClientRect().top - scroller.getBoundingClientRect().top,
          ),
          headerVisible:
            headerRect.top >= cardRect.top && headerRect.bottom <= cardRect.bottom,
          activeSection: document.activeElement?.getAttribute('data-settings-section'),
          samples: state.__settingsScrollSamples,
          hasIntermediateSample: state.__settingsScrollSamples.some(
            (sample) => sample > 0 && sample < finalTop,
          ),
        };
      });

      expect(result.cardScrollTop, `${size.width}px: hidden modal card scrolled`).toBe(0);
      expect(result.headerVisible, `${size.width}px: modal header left the card`).toBe(true);
      expect(result.contentScrollTop, `${size.width}px: content pane did not move`).toBeGreaterThan(0);
      expect(result.headingGap, `${size.width}px: heading missed the visual inset`).toBe(16);
      expect(result.activeSection).toBe('cli');
      expect(result.samples.length, `${size.width}px: no scroll journey was observed`).toBeGreaterThan(2);
      expect(result.hasIntermediateSample, `${size.width}px: navigation jumped without travel`).toBe(true);

      await page.keyboard.press('Escape');
      await expect(page.locator('#settings-root')).toHaveCount(0);
    }
  });

  test('active-section tracking follows the measured rule under a trusted wheel', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 700 });
    await openSettings(page);
    const scroller = page.locator('#settings-scroller');
    await scroller.hover();
    const observed: string[] = [];
    const push = async () => {
      const id = await activeSection(page);
      if (id && observed[observed.length - 1] !== id) observed.push(id);
    };
    await push();
    for (let step = 0; step < 40; step += 1) {
      await page.mouse.wheel(0, 220);
      await page.waitForFunction(() => {
        const el = document.querySelector('#settings-scroller')!;
        const previous = (window as unknown as { __prev?: number }).__prev;
        (window as unknown as { __prev?: number }).__prev = el.scrollTop;
        return previous !== el.scrollTop || el.scrollTop + el.clientHeight >= el.scrollHeight - 1;
      });
      await push();
    }
    // The rule: the LAST section whose heading crossed the anchor, ties by DOM
    // order, clamped to the last section at maximum scroll. So the observed
    // sequence is a strictly increasing walk through DOM order — never a jump
    // backwards, which is what callback-ordering would produce.
    const indices = observed.map((id) => SECTION_ORDER.indexOf(id as (typeof SECTION_ORDER)[number]));
    expect(indices.every((n) => n >= 0)).toBe(true);
    for (let i = 1; i < indices.length; i += 1) {
      expect(indices[i], `sequence went backwards: ${observed.join(' → ')}`)
        .toBeGreaterThan(indices[i - 1]);
    }
    expect(observed[0]).toBe('display');
    expect(observed[observed.length - 1]).toBe('cli');
    expect(observed.length).toBeGreaterThanOrEqual(4);
  });

  test('a dirty field the filter has removed still reaches the POST body', async ({ page }) => {
    await openSettings(page);
    const bodies: string[] = [];
    await page.route('**/api/settings', async (route) => {
      bodies.push(route.request().postData() ?? '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({}),
      });
    });
    const liveTail = page.locator('[data-settings-field="dashboard.live_tail"]');
    await liveTail.click();
    await expect(page.locator('#settings-save')).toHaveText(/1 change/);
    await page.locator('#settings-filter').fill('timezone');
    await expect(liveTail).toHaveCount(0); // removed from the DOM, not hidden
    await expect(page.locator('#settings-save')).toHaveText(/1 change/);
    await expect(page.locator('.settings-hidden-dirty')).toContainText(/hidden by this filter/i);
    await page.locator('#settings-save').click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(JSON.parse(bodies[0])).toEqual({ dashboard: { live_tail: false } });
  });

  test('a routed 400 reveals its filtered-away control and focuses it', async ({ page }) => {
    await openSettings(page);
    await page.route('**/api/settings', async (route) => {
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'live_tail must be a boolean', field: 'dashboard.live_tail' }),
      });
    });
    await page.locator('[data-settings-field="dashboard.live_tail"]').click();
    await page.locator('#settings-filter').fill('conversation');
    await page.locator('#settings-save').click();
    await expect(page.locator('.settings-error-summary')).toContainText('must be a boolean');
    // Filter the control away, then use the summary entry to get back to it.
    await page.locator('#settings-filter').fill('timezone');
    await expect(page.locator('[data-settings-field="dashboard.live_tail"]')).toHaveCount(0);
    await page.locator('.settings-error-link').first().click();
    await expect(page.locator('[data-settings-field="dashboard.live_tail"]')).toHaveCount(1);
    const focused = await page.evaluate(() =>
      document.activeElement?.getAttribute('data-settings-field'),
    );
    expect(focused).toBe('dashboard.live_tail');
  });

  test('a cleared numeric field emits no request and never replaces the stored value', async ({ page }) => {
    await openSettings(page);
    let requests = 0;
    await page.route('**/api/settings', async (route) => {
      requests += 1;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });
    const stored = await page.evaluate(() => {
      const raw = localStorage.getItem('ccusage.dashboard.prefs');
      return raw ? (JSON.parse(raw).sessionsPerPage ?? 100) : 100;
    });
    const perPage = page.locator('#settings-per-page');
    await perPage.click();
    // `fill('')` rather than a select-all chord: the chord's modifier differs
    // by platform, and what this case is about is the empty draft surviving,
    // not how it was emptied.
    await perPage.fill('');
    await expect(perPage).toHaveValue('');
    await page.locator('#settings-save').click();
    await expect(page.locator('.settings-error-summary')).toContainText(/between 10 and 1000/i);
    await expect(page.locator('#settings-root')).toBeVisible();
    expect(requests).toBe(0);
    // Typing a below-range value must not be rewritten to the minimum.
    await perPage.fill('5');
    await expect(perPage).toHaveValue('5');
    const after = await page.evaluate(() => {
      const raw = localStorage.getItem('ccusage.dashboard.prefs');
      return raw ? (JSON.parse(raw).sessionsPerPage ?? 100) : 100;
    });
    expect(after).toBe(stored);
  });

  test('the three dismissal paths keep their asymmetric contract', async ({ page }) => {
    await openSettings(page);
    // Escape while dirty raises the confirm, focuses the safe default, and
    // marks every sibling inert.
    await page.locator('[data-settings-field="dashboard.live_tail"]').click();
    await page.keyboard.press('Escape');
    const confirm = page.locator('[role="alertdialog"]');
    await expect(confirm).toBeVisible();
    await expect(page.locator('#settings-root')).toBeVisible();
    const focusedText = await page.evaluate(() => document.activeElement?.textContent ?? '');
    expect(focusedText).toMatch(/Keep editing/);
    const inert = await page.evaluate(() =>
      Array.from(document.querySelector('.modal-card')!.children)
        .filter((child) => !child.classList.contains('settings-confirm'))
        .every((child) => (child as HTMLElement).inert === true),
    );
    expect(inert).toBe(true);
    await page.getByRole('button', { name: /Keep editing/ }).click();
    await expect(confirm).toHaveCount(0);

    // The backdrop raises the same confirm.
    await page.locator('.modal-backdrop').click({ position: { x: 5, y: 5 } });
    await expect(page.locator('[role="alertdialog"]')).toBeVisible();
    await page.getByRole('button', { name: /Keep editing/ }).click();

    // Explicit Cancel discards immediately — no confirm.
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.locator('#settings-root')).toHaveCount(0);
  });

  // The third focus-loss path of §2.3, and the one only a real pointer can
  // construct: `#settings-tz-custom` is disabled in the DEFAULT state, so a
  // press on it has no focusable hit target and Chromium walks up for the
  // nearest focusable ancestor. Without one it focuses `<body>`, outside the
  // card, where the trap returns early and cannot bring focus back.
  test('a press on a disabled control never drops focus out of the dialog', async ({ page }) => {
    for (const size of [
      { width: 1440, height: 900 },
      { width: 390, height: 844 },
    ]) {
      await page.setViewportSize(size);
      await openSettings(page);
      const custom = page.locator('#settings-tz-custom');
      await expect(custom).toBeDisabled();
      // Somewhere real to fall FROM, so "focus never moved" cannot pass this.
      await page.locator('[data-settings-field="dashboard.live_tail"]').focus();
      // A raw mouse press at the control's own coordinates. Playwright's
      // `click()` refuses a disabled target even under `force`, and the point
      // of this case is the press itself, not the actionability contract.
      // Scrolling does not move focus, so the fall-from state survives it.
      await custom.scrollIntoViewIfNeeded();
      const box = (await custom.boundingBox())!;
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      const landed = await page.evaluate(() => {
        const card = document.querySelector('.modal-card')!;
        const active = document.activeElement;
        return {
          inside: !!active && active !== document.body && card.contains(active),
          tag: active?.nodeName ?? 'none',
        };
      });
      expect(landed.inside, `${size.width}px: focus fell to ${landed.tag}`).toBe(true);
      await page.keyboard.press('Escape');
      await expect(page.locator('#settings-root')).toHaveCount(0);
    }
  });

  test('opening Settings locks scroll on BOTH documentElement and body', async ({ page }) => {
    await page.goto('/');
    await page.locator('.dash-grid').waitFor();
    const before = await page.evaluate(() => ({
      html: getComputedStyle(document.documentElement).overflow,
      body: getComputedStyle(document.body).overflow,
    }));
    await page.keyboard.press('s');
    await page.locator('#settings-root').waitFor();
    const during = await page.evaluate(() => ({
      html: getComputedStyle(document.documentElement).overflow,
      body: getComputedStyle(document.body).overflow,
    }));
    expect(during.html).toBe('hidden');
    expect(during.body).toBe('hidden');
    await page.keyboard.press('Escape');
    await expect(page.locator('#settings-root')).toHaveCount(0);
    const after = await page.evaluate(() => ({
      html: getComputedStyle(document.documentElement).overflow,
      body: getComputedStyle(document.body).overflow,
    }));
    expect(after).toEqual(before);
  });

  test('all three control classes compute the app focus ring, with an unfocused canary', async ({ page }) => {
    await openSettings(page);
    // Establish keyboard modality so Chromium applies :focus-visible to a
    // programmatic focus; without it a mouse-modality page would not.
    await page.keyboard.press('Tab');
    const probe = async (selector: string) =>
      page.evaluate((sel) => {
        const element = document.querySelector<HTMLElement>(sel)!;
        element.focus();
        const style = getComputedStyle(element);
        return {
          width: style.outlineWidth,
          style: style.outlineStyle,
          color: style.outlineColor,
          offset: style.outlineOffset,
        };
      }, selector);

    const checkbox = await probe('[data-settings-field="dashboard.live_tail"]');
    const select = await probe('[data-settings-field="alerts.notifier"]');
    const text = await probe('#settings-per-page');
    // The dialog's own × is a fourth class: shared `.modal-close` chrome that
    // carries no focus rule of its own, so it kept Chromium's default blue.
    const close = await probe('#settings-root .modal-close');
    for (const [name, ring] of [
      ['checkbox', checkbox],
      ['select', select],
      ['number input', text],
      ['modal close', close],
    ] as const) {
      expect(ring.style, `${name} outline-style`).toBe('solid');
      expect(ring.width, `${name} outline-width`).toBe('2px');
      expect(ring.offset, `${name} outline-offset`).toBe('2px');
      expect(ring.color, `${name} outline-color`).toBe(checkbox.color);
    }
    // The canary: the probe discriminates, because an UNFOCUSED control of the
    // same class computes no ring.
    const unfocused = await page.evaluate(() => {
      const element = document.querySelector<HTMLElement>('#settings-filter-term')!;
      (document.activeElement as HTMLElement | null)?.blur();
      const style = getComputedStyle(element);
      return { style: style.outlineStyle, width: style.outlineWidth };
    });
    expect(unfocused.style === 'none' || unfocused.width === '0px').toBe(true);
  });

  test('no focused control intersects the action bar, including a parked state', async ({ page }) => {
    for (const size of [
      { width: 1440, height: 900 },
      { width: 390, height: 844 },
    ]) {
      await page.setViewportSize(size);
      await openSettings(page);
      // The deliberately constructed parked state: scroll so a mid-list control
      // sits exactly where a sticky bar would have covered it, then focus it.
      // Tabbing alone would not construct this — Chromium performs no
      // corrective scroll for a control already inside the scrollport.
      const overlaps = await page.evaluate(() => {
        const bar = document.querySelector('.settings-actions')!.getBoundingClientRect();
        const scroller = document.querySelector<HTMLElement>('#settings-scroller')!;
        const controls = Array.from(
          scroller.querySelectorAll<HTMLElement>(
            'input, select, button, [tabindex="-1"]',
          ),
        ).filter((el) => !(el as HTMLInputElement).disabled);
        const failures: string[] = [];
        for (const control of controls) {
          // Park it against the bottom edge of the scrollport.
          const scrollerRect = scroller.getBoundingClientRect();
          const controlRect = control.getBoundingClientRect();
          scroller.scrollTop += controlRect.top - (scrollerRect.bottom - controlRect.height);
          control.focus();
          const rect = control.getBoundingClientRect();
          const intersects =
            rect.bottom > bar.top &&
            rect.top < bar.bottom &&
            rect.right > bar.left &&
            rect.left < bar.right;
          if (intersects) {
            failures.push(
              `${control.tagName}${control.getAttribute('data-settings-field') ?? ''}`,
            );
          }
        }
        return { failures, checked: controls.length };
      });
      expect(overlaps.checked, `${size.width}px: nothing was probed`).toBeGreaterThan(5);
      expect(overlaps.failures, `${size.width}px`).toEqual([]);
      await page.keyboard.press('Escape');
    }
  });

  // §4.4 removed the occlusion by moving the action bar out of the scrollport.
  // Three superseded rules that still SAID "sticky" were deleted with it; these
  // are the exact properties they set, so a deletion that changed anything
  // visible would move one of these numbers.
  test('the action bar is static and the scrollport keeps its own bottom padding', async ({ page }) => {
    for (const size of [
      { width: 1440, height: 900 },
      { width: 390, height: 844 },
    ]) {
      await page.setViewportSize(size);
      await openSettings(page);
      const computed = await page.evaluate(() => {
        const bar = getComputedStyle(document.querySelector('.settings-actions')!);
        const scroller = getComputedStyle(document.querySelector('#settings-scroller')!);
        return {
          position: bar.position,
          gap: bar.columnGap,
          marginTop: bar.marginTop,
          marginLeft: bar.marginLeft,
          scrollerPaddingBottom: scroller.paddingBottom,
        };
      });
      expect(computed.position, `${size.width}px`).toBe('static');
      expect(computed.gap, `${size.width}px`).toBe('10px');
      expect(computed.marginTop, `${size.width}px`).toBe('0px');
      expect(computed.marginLeft, `${size.width}px`).toBe('0px');
      expect(computed.scrollerPaddingBottom, `${size.width}px`).toBe('16px');
      await page.keyboard.press('Escape');
    }
  });

  // §4.4's defect on the OTHER axis. Below 768px the rail is a horizontally
  // scrolling strip, and the browser performed no corrective scroll for a
  // focused entry past its right edge: measured at 390px, "Conversation viewer"
  // was 23.6% visible with 120.2px clipped and `scrollLeft` stuck at 0.
  test('every rail entry is fully visible once focused, at 390px', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openSettings(page);
    // The precondition: the strip really does overflow here, or the assertion
    // below would hold against a rail that never needed to scroll.
    const overflow = await page.evaluate(() => {
      const rail = document.querySelector<HTMLElement>('.settings-rail')!;
      return { scrollWidth: rail.scrollWidth, clientWidth: rail.clientWidth };
    });
    expect(
      overflow.scrollWidth,
      `the rail does not overflow at 390px (${overflow.scrollWidth} <= ${overflow.clientWidth})`,
    ).toBeGreaterThan(overflow.clientWidth + 1);

    const count = await page.locator('.settings-rail-link').count();
    expect(count).toBeGreaterThan(5);
    const report: { title: string; visible: number; clipped: number }[] = [];
    for (let index = 0; index < count; index += 1) {
      await page.locator('.settings-rail-link').nth(index).focus();
      report.push(
        await page.evaluate((i) => {
          const rail = document.querySelector<HTMLElement>('.settings-rail')!;
          const link = document.querySelectorAll<HTMLElement>('.settings-rail-link')[i];
          const railRect = rail.getBoundingClientRect();
          const rect = link.getBoundingClientRect();
          const overlap = Math.max(
            0,
            Math.min(rect.right, railRect.right) - Math.max(rect.left, railRect.left),
          );
          return {
            title: link.textContent?.trim() ?? '',
            visible: rect.width === 0 ? 0 : overlap / rect.width,
            clipped: rect.width - overlap,
          };
        }, index),
      );
    }
    for (const row of report) {
      expect(
        row.visible,
        `${row.title}: ${(row.visible * 100).toFixed(1)}% visible, ${row.clipped.toFixed(1)}px clipped`,
      ).toBeGreaterThan(0.99);
    }
  });

  // EVERY placeholder in the dialog, at BOTH viewports, measured on rendered
  // pixels.
  //
  // The previous form of this case measured one placeholder against the input's
  // own computed font through `canvas.measureText`. That reported 227.6px of
  // text in a 228.8px content box — 1.2px of headroom — for a string the
  // browser then clipped mid-word at BOTH viewports, because a font
  // reconstruction is not what the layout engine paints. Placing the same
  // string in the field reports the overflow directly, and this form of the
  // test measured it as 44px over a 218px field at 1440px and 13px over a 249px
  // field at 390px. So the probe now asks the engine, and it asks about every
  // placeholder the dialog renders rather than the one the audit named.
  for (const size of [
    { width: 1440, height: 900 },
    { width: 390, height: 844 },
  ]) {
    test(`every placeholder in the dialog renders whole at ${size.width}px`, async ({ page }) => {
      await page.setViewportSize(size);
      await openSettings(page);
      const rows = await page.evaluate(() => {
        const root = document.querySelector('#settings-root')!;
        // The native setter, so a controlled React input takes the probe text
        // without an `input` event that would re-render the dialog and move the
        // very box being measured.
        const setValue = (input: HTMLInputElement, value: string) => {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(
            input,
            value,
          );
        };
        return Array.from(root.querySelectorAll<HTMLInputElement>('input[placeholder]')).map(
          (input) => {
            const original = input.value;
            const measure = (text: string) => {
              setValue(input, text);
              return input.scrollWidth - input.clientWidth;
            };
            const overflow = measure(input.placeholder);
            // A flex-grown field can be a fractional number of pixels wide, and
            // `clientWidth` floors while `scrollWidth` ceils, so such a field
            // reports 1px of overflow for ANY content. Measuring one character
            // — which cannot overflow a field this size — gives that element's
            // own rounding, so it can be subtracted instead of papered over
            // with a constant tolerance that would also hide a real 1px clip on
            // a field whose width happens to be integral.
            const rounding = measure('M');
            // The same field, deliberately overfilled. A probe that cannot see
            // THIS overflow could not have seen the real one either.
            const canary = measure(`${input.placeholder} ${'M'.repeat(40)}`);
            setValue(input, original);
            return {
              id: input.id || input.getAttribute('data-settings-field') || input.name,
              placeholder: input.placeholder,
              rendered: input.getClientRects().length > 0,
              width: input.clientWidth,
              rounding,
              overflow: overflow - rounding,
              canary: canary - rounding,
            };
          },
        );
      });
      // Non-vacuity, and the reason it is a count rather than a presence check:
      // a sweep that silently found nothing to measure would otherwise pass.
      expect(rows.length, 'no placeholder was examined').toBeGreaterThanOrEqual(4);
      expect(
        rows.map((row) => row.id),
        'the filter placeholder is missing from the sweep',
      ).toContain('settings-filter');
      for (const row of rows) {
        // A control the sweep cannot measure is a failure, never a skip.
        expect(row.rendered, `${row.id} is not rendered, so nothing was measured`).toBe(true);
        // The subtracted rounding is a sub-pixel artifact and can only be 0 or
        // 1. Anything larger means one character does not fit, and subtracting
        // it would hide a real clip rather than a rounding step.
        expect(
          row.rounding,
          `${row.id}: one character already overflows the field by ${row.rounding}px`,
        ).toBeLessThanOrEqual(1);
        expect(
          row.canary,
          `${row.id}: the probe reports no overflow even when overfilled`,
        ).toBeGreaterThan(0);
        expect(
          row.overflow,
          `${row.id}: “${row.placeholder}” overflows its ${row.width}px field by ${row.overflow}px`,
        ).toBeLessThanOrEqual(0);
      }
    });
  }

  // `.is-changed` was applied on eight fieldsets and targeted by no rule, so
  // the class asserted a state it could not show.
  test('a fieldset holding unsaved state is visually distinct from a clean one', async ({ page }) => {
    await openSettings(page);
    const before = await page.evaluate(() => {
      const fs = document
        .querySelector('[data-settings-field="dashboard.live_tail"]')!
        .closest('fieldset')!;
      return getComputedStyle(fs).borderTopColor;
    });
    await page.locator('[data-settings-field="dashboard.live_tail"]').click();
    const after = await page.evaluate(() => {
      const fs = document
        .querySelector('[data-settings-field="dashboard.live_tail"]')!
        .closest('fieldset')!;
      return {
        className: fs.className,
        borderTopColor: getComputedStyle(fs).borderTopColor,
      };
    });
    expect(after.className).toContain('is-changed');
    expect(
      after.borderTopColor,
      `is-changed computes the same border as clean (${before})`,
    ).not.toBe(before);
  });
});

// #513 S2 AC4 — EVERY disabled control in this dialog is visually distinct from
// an enabled one.
//
// Two earlier forms of this case each measured a SUBSET, and each passed while
// a real control looked exactly like an operable one. The first skipped every
// non-BUTTON row, so it missed the disabled `#settings-tz-custom`. The second
// compared each disabled control against an enabled twin OF ITS OWN FAMILY, so
// it never built a candidate set containing `.modal-close` — shared modal
// chrome that belongs to no settings family — and the header `×` computed
// opacity 1, `rgb(217,221,229)` and `cursor: pointer` while disabled mid-save.
//
// So the baseline is no longer a twin. Each control is compared against ITSELF
// with the disabled state removed, which exists for every control by
// construction and can never be missing, and the sweep enumerates the dialog
// subtree by STATE (`:disabled` plus `[aria-disabled="true"]`) rather than by a
// list of families.

/** One disabled control, measured against its own enabled appearance. */
type DisabledProbe = {
  label: string;
  ariaOnly: boolean;
  /** `<option>` / `<optgroup>`: painted by the browser's own popup, not by us. */
  nativePopup: boolean;
  attributeDisabled: boolean;
  differs: string[];
  effectiveOpacity: number;
  cursor: string;
  pointerEvents: string;
};

async function sweepDisabled(page: Page): Promise<DisabledProbe[]> {
  return page.evaluate(() => {
    const root = document.querySelector('#settings-root')!;
    // The dimming a checkbox row carries lives on its label, not on the 13px
    // box, so what a person sees is the product down the ancestor chain.
    const effectiveOpacity = (element: Element) => {
      let value = 1;
      let node: Element | null = element;
      while (node && node !== document.documentElement) {
        value *= Number(getComputedStyle(node).opacity);
        node = node.parentElement;
      }
      return value;
    };
    const read = (element: Element) => {
      const style = getComputedStyle(element);
      return {
        effectiveOpacity: effectiveOpacity(element),
        color: style.color,
        background: style.backgroundColor,
        borderColor: style.borderTopColor,
        cursor: style.cursor,
        pointerEvents: style.pointerEvents,
      };
    };
    const visual = ['effectiveOpacity', 'color', 'background', 'borderColor'] as const;
    return Array.from(
      root.querySelectorAll<HTMLElement>(':disabled, [aria-disabled="true"]'),
    ).map((element) => {
      const ariaOnly = !element.matches(':disabled');
      const disabled = read(element);
      // Its own enabled appearance. Reading a computed style forces the style
      // recalculation, including the `label:has(input:disabled)` rule that dims
      // a checkbox row from above, and the state is restored before this
      // function returns, so React never observes the flip.
      if (ariaOnly) element.removeAttribute('aria-disabled');
      else (element as HTMLInputElement).disabled = false;
      const enabled = read(element);
      if (ariaOnly) element.setAttribute('aria-disabled', 'true');
      else (element as HTMLInputElement).disabled = true;
      const text = element.textContent?.trim() ?? '';
      return {
        label:
          element.getAttribute('data-settings-field') ||
          element.id ||
          `${element.tagName.toLowerCase()}.${Array.from(element.classList).join('.')}` +
            (text ? `[${text.slice(0, 12)}]` : ''),
        ariaOnly,
        nativePopup: element.tagName === 'OPTION' || element.tagName === 'OPTGROUP',
        attributeDisabled: element.hasAttribute('disabled'),
        differs: visual.filter((key) => disabled[key] !== enabled[key]),
        effectiveOpacity: disabled.effectiveOpacity,
        cursor: disabled.cursor,
        pointerEvents: disabled.pointerEvents,
      };
    });
  });
}

/**
 * Every offender in ONE list rather than one `expect` per property, because
 * Playwright abandons a test at its first failed assertion and a class-level
 * sweep whose report stops at the first row is back to naming instances.
 */
function distinctnessFailures(rows: DisabledProbe[], state: string): string[] {
  const failures: string[] = [];
  const fail = (row: DisabledProbe, why: string) =>
    failures.push(`${state}: ${row.label} ${why}`);
  for (const row of rows) {
    if (row.nativePopup) {
      // A disabled `<option>` is drawn by the browser inside the select's own
      // popup, where author CSS does not reach and a computed style read from
      // the document says nothing about what a person sees. The only contract
      // available is that the attribute is there for the browser to act on, so
      // that is what is checked — and the partition is a fixed pair of tag
      // names, so no CONTROL can ever land on this side of it.
      if (!row.attributeDisabled) fail(row, 'is aria-disabled only inside a native popup');
      continue;
    }
    if (row.differs.length === 0) {
      fail(row, 'computes the same appearance enabled and disabled');
    }
    if (!(row.effectiveOpacity < 1)) {
      fail(row, `is not dimmed (effective opacity ${row.effectiveOpacity})`);
    }
    if (row.cursor === 'pointer') fail(row, 'still shows a pointer cursor');
    // `disabled` means the control cannot be operated at all. `aria-disabled`
    // deliberately keeps the control reachable so a reader can find it and hear
    // why, so it is held only to the visual contract.
    if (!row.ariaOnly && row.pointerEvents !== 'none') {
      fail(row, 'still accepts pointer events');
    }
  }
  return failures;
}

test('every disabled control is visually distinct, in the default state and mid-save', async ({
  page,
}) => {
  await openSettings(page);
  const initial = await sweepDisabled(page);
  // Non-vacuity, counted over the controls this page actually paints so the
  // native-popup rows cannot stand in for them.
  expect(
    initial.filter((row) => !row.nativePopup).length,
    'the default state disabled nothing, so the sweep proves nothing',
  ).toBeGreaterThanOrEqual(2);
  expect(
    initial.map((row) => row.label),
    'the default-state disabled text input is missing from the sweep',
  ).toContain('display.tz');
  expect(distinctnessFailures(initial, 'default state')).toEqual([]);

  // The in-flight-save state, which a default-state sweep can never reach: the
  // header `×` and Cancel are disabled only while a submit is outstanding. Hold
  // the response open so that state is observable rather than instantaneous.
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/api/settings', async (route) => {
    await held;
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
  });
  await page.locator('[data-settings-field="dashboard.live_tail"]').click();
  await page.locator('#settings-save').click();
  await expect(page.locator('#settings-save')).toHaveText(/Saving/);
  const inFlight = await sweepDisabled(page);
  release();

  expect(
    inFlight.filter((row) => !row.nativePopup).length,
    'the in-flight state disabled nothing',
  ).toBeGreaterThanOrEqual(3);
  const labels = inFlight.map((row) => row.label);
  expect(
    labels.filter((label) => label.startsWith('button.modal-close')),
    `the header × is missing from the in-flight sweep: ${labels.join(', ')}`,
  ).toHaveLength(1);
  expect(distinctnessFailures(inFlight, 'mid-save')).toEqual([]);
});
