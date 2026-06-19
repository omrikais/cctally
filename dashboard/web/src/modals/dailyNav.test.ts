import { describe, it, expect } from 'vitest';
import { stepDay } from './dailyNav';
import type { DailyPanelRow } from '../types/envelope';

// stepDay only reads `.date`; cast minimal objects.
const rows = (['2026-06-05', '2026-06-04', '2026-06-03'] as const).map(
  (date) => ({ date }) as DailyPanelRow,
); // newest-first, matching the envelope + DailyModal's `rows`

describe('stepDay', () => {
  it("'older' returns the next-older day (idx+1 in newest-first rows)", () => {
    expect(stepDay(rows, '2026-06-04', 'older')).toBe('2026-06-03');
  });
  it("'newer' returns the next-newer day (idx-1)", () => {
    expect(stepDay(rows, '2026-06-04', 'newer')).toBe('2026-06-05');
  });
  it("returns null at the oldest boundary for 'older'", () => {
    expect(stepDay(rows, '2026-06-03', 'older')).toBeNull();
  });
  it("returns null at the newest boundary for 'newer'", () => {
    expect(stepDay(rows, '2026-06-05', 'newer')).toBeNull();
  });
  it('returns null when the current date is not in rows', () => {
    expect(stepDay(rows, '2026-01-01', 'older')).toBeNull();
  });
  it('returns null when there is no current date', () => {
    expect(stepDay(rows, null, 'older')).toBeNull();
  });
});
