import { describe, expect, it } from 'vitest';
import { cacheReportChartSlot } from './cacheReportChartSlots';

describe('Cache Report shared x-slot contract (#452)', () => {
  it.each(['mini', 'large'] as const)('%s centers a one-row window', (size) => {
    const slot = cacheReportChartSlot(size, 0, 1);
    expect(slot.center).toBe(slot.chartWidth / 2);
  });

  it.each(['mini', 'large'] as const)(
    '%s keeps every full-window slot inside the chart',
    (size) => {
      const slots = Array.from({ length: 14 }, (_, index) =>
        cacheReportChartSlot(size, index, 14));
      expect(slots[0]!.left).toBeGreaterThanOrEqual(0);
      expect(slots[13]!.right).toBeLessThanOrEqual(slots[13]!.chartWidth);
      expect(slots.map(({ center }) => center)).toEqual(
        [...slots].map(({ center }) => center).sort((a, b) => a - b),
      );
    },
  );

  it('rejects invalid indices rather than inventing coordinates', () => {
    expect(() => cacheReportChartSlot('large', 14, 14)).toThrow(RangeError);
    expect(() => cacheReportChartSlot('mini', 0, 0)).toThrow(RangeError);
  });
});
