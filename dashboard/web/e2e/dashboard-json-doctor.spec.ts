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
