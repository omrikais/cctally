import { expect, test } from '@playwright/test';

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
] as const;

for (const viewport of VIEWPORTS) {
  test(`strict Doctor report renders at ${viewport.name}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const consoleErrors: string[] = [];
    const failedRequests: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    page.on('requestfailed', (request) => {
      failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText ?? 'failed'}`);
    });

    await page.goto('/');
    await expect(page.locator('#main-content')).toBeVisible();

    const doctorWire = await page.evaluate(async () => {
      const response = await fetch('/api/doctor');
      const text = await response.text();
      return {
        status: response.status,
        text,
        parsed: JSON.parse(text) as Record<string, unknown>,
      };
    });
    expect(doctorWire.status).toBe(200);
    expect(doctorWire.text).not.toMatch(/(?:NaN|-?Infinity)/);
    expect(doctorWire.parsed).toHaveProperty('categories');

    await page.keyboard.press('d');
    const modal = page.locator('.doctor-modal-card');
    await expect(modal).toBeVisible();
    await expect(modal.locator('.doctor-modal__summary')).toContainText(/OK.*WARN.*FAIL/);
    const pipeline = modal.locator('.doctor-modal__check').filter({
      hasText: 'Statusline pipeline',
    });
    await expect(pipeline).toContainText('no recent regular-pool timer observed');
    await pipeline.getByRole('button', { name: /details/i }).click();
    await expect(pipeline.locator('.doctor-modal__details')).toContainText(
      '"transport_age_seconds": null',
    );
    await expect(pipeline.locator('.doctor-modal__details')).toContainText(
      '"selected_age_seconds": null',
    );

    const overflow = await page.evaluate(() => {
      const card = document.querySelector('.doctor-modal-card') as HTMLElement;
      return {
        documentX: document.documentElement.scrollWidth - window.innerWidth,
        cardX: card.scrollWidth - card.clientWidth,
        cardBottom: card.getBoundingClientRect().bottom - window.innerHeight,
      };
    });
    expect(overflow.documentX).toBeLessThanOrEqual(1);
    expect(overflow.cardX).toBeLessThanOrEqual(1);
    expect(overflow.cardBottom).toBeLessThanOrEqual(1);

    await page.screenshot({
      path: testInfo.outputPath(`doctor-${viewport.name}.png`),
      fullPage: false,
    });
    expect(consoleErrors).toEqual([]);
    expect(failedRequests).toEqual([]);
  });
}

// #933 — the first-run onboarding toast is live for 8 s on a fresh client. At
// 390×844 it sat above the Doctor sheet and swallowed taps on the bottom rows;
// it must paint below the dialog layer at every width.
for (const viewport of VIEWPORTS) {
  test(`Doctor rows stay reachable while the onboarding toast is live at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto('/');
    const toast = page.locator('.onboarding-toast');
    await expect(toast).toBeVisible();

    await page.keyboard.press('d');
    await expect(page.locator('.doctor-modal-card .doctor-modal__summary')).toBeVisible();
    await page.evaluate(async () => {
      const card = document.querySelector('.doctor-modal-card') as HTMLElement;
      const finite = card
        .getAnimations({ subtree: true })
        .filter((a) => a.effect?.getComputedTiming().iterations !== Infinity);
      await Promise.all(finite.map((a) => a.finished));
    });

    await page.evaluate(() => {
      for (const header of document.querySelectorAll<HTMLElement>('.doctor-modal__category-header')) {
        if (header.getAttribute('aria-expanded') === 'false') header.click();
      }
    });
    await page.waitForFunction(() =>
      [...document.querySelectorAll('.doctor-modal__category-header')].every(
        (header) => header.getAttribute('aria-expanded') === 'true',
      ),
    );
    const body = await page.evaluate(() => {
      const el = document.querySelector('.doctor-modal-card .modal-body') as HTMLElement;
      return { scrollHeight: el.scrollHeight, clientHeight: el.clientHeight };
    });
    expect(body.scrollHeight).toBeGreaterThan(body.clientHeight);

    // The toast's 8 s window must still be open, or the probes below would
    // measure nothing (a readable failure instead of a null dereference).
    await expect(toast, 'the onboarding toast is still live').toBeVisible();
    const probes = await page.evaluate(() => {
      const t = document.querySelector('.onboarding-toast')!.getBoundingClientRect();
      const controls = document.querySelectorAll<HTMLElement>(
        '.doctor-modal__category-header, .doctor-modal__details-toggle',
      );
      const seen = { header: 0, details: 0 };
      return [...controls].map((el) => {
        el.scrollIntoView({ block: 'end', behavior: 'instant' });
        const r = el.getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const kind = el.classList.contains('doctor-modal__category-header') ? 'header' : 'details';
        const name = kind === 'header'
          ? el.textContent?.trim()
          : el.closest('.doctor-modal__check')?.querySelector('strong')?.textContent?.trim();
        return {
          kind,
          label: `${kind} ${seen[kind]++}: ${name?.slice(0, 40) ?? ''}`,
          hit: el.contains(document.elementFromPoint(cx, cy)),
          underToast: cx >= t.left && cx <= t.right && cy >= t.top && cy <= t.bottom,
        };
      });
    });
    expect(probes.filter((p) => !p.hit).map((p) => p.label), 'controls not their own hit target').toEqual([]);

    if (viewport.name === 'mobile') {
      const covered = probes.findIndex((p) => p.kind === 'header' && p.underToast);
      expect(covered, 'a category header under the toast').toBeGreaterThanOrEqual(0);
      // `probes` lists headers and toggles in document order; map back to the header's index.
      const headerIndex = probes.slice(0, covered).filter((p) => p.kind === 'header').length;
      const header = page.locator('.doctor-modal__category-header').nth(headerIndex);
      const centre = await header.evaluate((el) => {
        el.scrollIntoView({ block: 'end', behavior: 'instant' });
        const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      });
      await page.mouse.click(centre.x, centre.y);
      await expect(header).toHaveAttribute('aria-expanded', 'false');
    }

    await expect(toast, 'the onboarding toast is still live').toBeVisible();
    const toastCentreHitsToast = await page.evaluate(() => {
      const el = document.querySelector('.onboarding-toast')!;
      const r = el.getBoundingClientRect();
      return el.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2));
    });
    expect(toastCentreHitsToast, 'the toast paints above the dialog layer').toBe(false);
    await expect(toast).toBeVisible();
  });
}
