import { describe, it, expect } from 'vitest';
import { stepPeriod, keyOf } from './periodNav';

const rows = [ // newest-first
  { key: '2026-07-02' }, { key: '2026-07-01' }, { key: '2026-06-30' },
];

describe('stepPeriod', () => {
  it('older → next index', () => expect(stepPeriod(rows, '2026-07-02', 'older')).toBe('2026-07-01'));
  it('newer → prev index', () => expect(stepPeriod(rows, '2026-07-01', 'newer')).toBe('2026-07-02'));
  it('older at oldest boundary → null', () => expect(stepPeriod(rows, '2026-06-30', 'older')).toBeNull());
  it('newer at newest boundary → null', () => expect(stepPeriod(rows, '2026-07-02', 'newer')).toBeNull());
  it('unknown key → null', () => expect(stepPeriod(rows, 'nope', 'older')).toBeNull());
  it('null current → null', () => expect(stepPeriod(rows, null, 'older')).toBeNull());
});

describe('keyOf', () => {
  it('day → date', () => expect(keyOf({ date: '2026-07-02' } as any, 'day')).toBe('2026-07-02'));
  it('week → week_start_at', () => expect(keyOf({ week_start_at: '2026-06-30T00:00:00Z' } as any, 'week')).toBe('2026-06-30T00:00:00Z'));
  it('week → falls back to label when week_start_at is absent', () => expect(keyOf({ label: '2026-W26' } as any, 'week')).toBe('2026-W26'));
  it('month → label', () => expect(keyOf({ label: '2026-06' } as any, 'month')).toBe('2026-06'));
});
