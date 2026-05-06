import { describe, it, expect } from 'vitest';
import {
  applyTableSort,
  nextSortOverride,
  type SortOverride,
  type TableColumn,
} from '../src/lib/tableSort';

interface Row { id: string; n: number; s: string }

const COLS: TableColumn<Row>[] = [
  { id: 'n', label: 'N', defaultDirection: 'desc',
    compare: (a, b) => a.n - b.n },
  { id: 's', label: 'S', defaultDirection: 'asc',
    compare: (a, b) => a.s.localeCompare(b.s) },
];

const ROWS: Row[] = [
  { id: 'a', n: 2, s: 'banana' },
  { id: 'b', n: 1, s: 'cherry' },
  { id: 'c', n: 3, s: 'apple' },
];

describe('applyTableSort', () => {
  it('returns input unchanged when override is null', () => {
    expect(applyTableSort(ROWS, COLS, null)).toEqual(ROWS);
  });

  it('sorts ascending when override.direction is asc', () => {
    const out = applyTableSort(ROWS, COLS, { column: 'n', direction: 'asc' });
    expect(out.map((r) => r.id)).toEqual(['b', 'a', 'c']);
  });

  it('sorts descending when override.direction is desc', () => {
    const out = applyTableSort(ROWS, COLS, { column: 'n', direction: 'desc' });
    expect(out.map((r) => r.id)).toEqual(['c', 'a', 'b']);
  });

  it('returns input unchanged when override.column is unknown', () => {
    const out = applyTableSort(ROWS, COLS, { column: 'nope', direction: 'asc' });
    expect(out).toEqual(ROWS);
  });

  it('does not mutate the input array', () => {
    const copy = ROWS.slice();
    applyTableSort(ROWS, COLS, { column: 'n', direction: 'desc' });
    expect(ROWS).toEqual(copy);
  });
});

describe('nextSortOverride', () => {
  const NCOL = { id: 'n', defaultDirection: 'desc' as const };
  const SCOL = { id: 's', defaultDirection: 'asc' as const };

  it('seeds with column defaultDirection when starting from null', () => {
    expect(nextSortOverride(null, NCOL)).toEqual({ column: 'n', direction: 'desc' });
    expect(nextSortOverride(null, SCOL)).toEqual({ column: 's', direction: 'asc' });
  });

  it('flips direction on second click of same column (desc-default)', () => {
    const cur: SortOverride = { column: 'n', direction: 'desc' };
    expect(nextSortOverride(cur, NCOL)).toEqual({ column: 'n', direction: 'asc' });
  });

  it('flips direction on second click of same column (asc-default)', () => {
    const cur: SortOverride = { column: 's', direction: 'asc' };
    expect(nextSortOverride(cur, SCOL)).toEqual({ column: 's', direction: 'desc' });
  });

  it('clears on third click of same column (desc-default)', () => {
    const cur: SortOverride = { column: 'n', direction: 'asc' };  // already flipped
    expect(nextSortOverride(cur, NCOL)).toBeNull();
  });

  it('clears on third click of same column (asc-default)', () => {
    const cur: SortOverride = { column: 's', direction: 'desc' };  // already flipped
    expect(nextSortOverride(cur, SCOL)).toBeNull();
  });

  it('switching to a different column resets to that column defaultDirection', () => {
    const cur: SortOverride = { column: 'n', direction: 'asc' };
    expect(nextSortOverride(cur, SCOL)).toEqual({ column: 's', direction: 'asc' });
  });
});

import { SESSIONS_COLUMNS } from '../src/lib/sessionsColumns';
import type { SessionRow } from '../src/types/envelope';

describe('SESSIONS_COLUMNS registry', () => {
  it('exposes the five columns matching the visible table', () => {
    expect(SESSIONS_COLUMNS.map((c) => c.id)).toEqual([
      'started', 'duration', 'model', 'project', 'cost',
    ]);
  });

  it('uses smart per-column defaultDirection (numeric/temporal desc, text asc)', () => {
    const map = Object.fromEntries(
      SESSIONS_COLUMNS.map((c) => [c.id, c.defaultDirection]),
    );
    expect(map.started).toBe('desc');
    expect(map.duration).toBe('desc');
    expect(map.cost).toBe('desc');
    expect(map.model).toBe('asc');
    expect(map.project).toBe('asc');
  });

  it('cost comparator orders by cost_usd ascending', () => {
    const cost = SESSIONS_COLUMNS.find((c) => c.id === 'cost')!;
    const a: SessionRow = {
      session_id: 'a', started_utc: null, duration_min: 1, model: 'm', project: 'p', cost_usd: 1.0,
    };
    const b: SessionRow = { ...a, session_id: 'b', cost_usd: 2.0 };
    expect(cost.compare(a, b)).toBeLessThan(0);
    expect(cost.compare(b, a)).toBeGreaterThan(0);
  });

  it('started comparator orders by parsed timestamp ascending', () => {
    const started = SESSIONS_COLUMNS.find((c) => c.id === 'started')!;
    const a: SessionRow = {
      session_id: 'a', started_utc: '2026-04-27T10:00:00Z', duration_min: 1,
      model: 'm', project: 'p', cost_usd: 0,
    };
    const b: SessionRow = { ...a, session_id: 'b', started_utc: '2026-04-28T10:00:00Z' };
    expect(started.compare(a, b)).toBeLessThan(0);
  });

  it('null-safe: cost comparator treats null cost_usd as 0', () => {
    const cost = SESSIONS_COLUMNS.find((c) => c.id === 'cost')!;
    const a: SessionRow = {
      session_id: 'a', started_utc: null, duration_min: 1, model: 'm', project: 'p', cost_usd: null,
    };
    const b: SessionRow = { ...a, session_id: 'b', cost_usd: 5.0 };
    expect(cost.compare(a, b)).toBeLessThan(0);
  });

  it('started comparator stays transitive with null started_utc rows interleaved', () => {
    const started = SESSIONS_COLUMNS.find((c) => c.id === 'started')!;
    const rows: SessionRow[] = [
      { session_id: 'a', started_utc: '2026-01-02T00:00:00Z', duration_min: 1, model: 'm', project: 'p', cost_usd: 0 },
      { session_id: 'x', started_utc: null,                     duration_min: 1, model: 'm', project: 'p', cost_usd: 0 },
      { session_id: 'b', started_utc: '2026-01-01T00:00:00Z', duration_min: 1, model: 'm', project: 'p', cost_usd: 0 },
      { session_id: 'y', started_utc: null,                     duration_min: 1, model: 'm', project: 'p', cost_usd: 0 },
      { session_id: 'c', started_utc: '2026-01-03T00:00:00Z', duration_min: 1, model: 'm', project: 'p', cost_usd: 0 },
    ];
    const sorted = rows.slice().sort(started.compare);
    // Filter to non-null rows only — their relative order MUST be ascending by date.
    const nonNullIds = sorted.filter((r) => r.started_utc).map((r) => r.session_id);
    expect(nonNullIds).toEqual(['b', 'a', 'c']);
  });
});

import { TREND_COLUMNS, type TrendTableRow } from '../src/lib/trendColumns';

describe('TREND_COLUMNS registry', () => {
  it('exposes the four columns matching the trend table', () => {
    expect(TREND_COLUMNS.map((c) => c.id)).toEqual([
      'week', 'used_pct', 'dollar_per_pct', 'delta',
    ]);
  });

  it('all columns default to desc (numeric/temporal first-click)', () => {
    expect(TREND_COLUMNS.every((c) => c.defaultDirection === 'desc')).toBe(true);
  });

  it('week comparator uses _chronoIdx (envelope position), not label', () => {
    const week = TREND_COLUMNS.find((c) => c.id === 'week')!;
    // Year-wrap scenario: lex-order would put "12-29" after "01-05",
    // but the December row is chronologically EARLIER (lower _chronoIdx).
    const dec: TrendTableRow = {
      label: '12-29', used_pct: null, dollar_per_pct: null, delta: null,
      is_current: false, _chronoIdx: 0,
    };
    const jan: TrendTableRow = { ...dec, label: '01-05', _chronoIdx: 7, is_current: true };
    expect(week.compare(dec, jan)).toBeLessThan(0);  // dec first when ascending
  });

  it('used_pct comparator orders by used_pct ascending; null treated as 0', () => {
    const used = TREND_COLUMNS.find((c) => c.id === 'used_pct')!;
    const a: TrendTableRow = {
      label: 'a', used_pct: 10, dollar_per_pct: null, delta: null,
      is_current: false, _chronoIdx: 0,
    };
    const b: TrendTableRow = { ...a, label: 'b', used_pct: 50 };
    const c: TrendTableRow = { ...a, label: 'c', used_pct: null };
    expect(used.compare(a, b)).toBeLessThan(0);
    expect(used.compare(c, a)).toBeLessThan(0);  // null (0) < 10
  });
});
