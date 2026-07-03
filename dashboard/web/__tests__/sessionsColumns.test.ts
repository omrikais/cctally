import { describe, it, expect } from 'vitest';
import { sessionsColumns, ALL_SESSIONS_COLUMNS } from '../src/lib/sessionsColumns';
import type { SessionRow } from '../src/types/envelope';

const ids = (cols: { id: string }[]) => cols.map((c) => c.id);

describe('sessionsColumns', () => {
  it('superset carries every column in canonical order', () => {
    expect(ids(ALL_SESSIONS_COLUMNS)).toEqual(
      ['started', 'duration', 'model', 'session', 'project', 'cache', 'cost'],
    );
  });

  it('multi-model keeps Model; order started,dur,model,session,project,cache,cost', () => {
    expect(ids(sessionsColumns({ oneModel: false, transcriptsOn: true }))).toEqual(
      ['started', 'duration', 'model', 'session', 'project', 'cache', 'cost'],
    );
  });

  it('single-model drops Model entirely', () => {
    expect(ids(sessionsColumns({ oneModel: true, transcriptsOn: true }))).toEqual(
      ['started', 'duration', 'session', 'project', 'cache', 'cost'],
    );
  });

  it('Session + Cache are always present regardless of the transcript gate', () => {
    const off = ids(sessionsColumns({ oneModel: false, transcriptsOn: false }));
    expect(off).toContain('session');
    expect(off).toContain('cache');
    const on = ids(sessionsColumns({ oneModel: true, transcriptsOn: false }));
    expect(on).toContain('session');
    expect(on).toContain('cache');
  });

  it('session comparator orders by title (localeCompare), asc default', () => {
    const session = ALL_SESSIONS_COLUMNS.find((c) => c.id === 'session')!;
    expect(session.defaultDirection).toBe('asc');
    const a: SessionRow = {
      session_id: 'a', started_utc: null, duration_min: 1, model: 'm',
      project: 'p', project_key: null, cost_usd: 0, title: 'Alpha',
    };
    const b: SessionRow = { ...a, session_id: 'b', title: 'Beta' };
    expect(session.compare(a, b)).toBeLessThan(0);
    // null/undefined title collates as '' — sorts first, never throws.
    const n: SessionRow = { ...a, session_id: 'n', title: null };
    expect(session.compare(n, a)).toBeLessThan(0);
  });

  it('cache column parks null cache_hit_pct via nullKey (direction-invariant)', () => {
    const cache = ALL_SESSIONS_COLUMNS.find((c) => c.id === 'cache')!;
    expect(cache.numeric).toBe(true);
    expect(cache.defaultDirection).toBe('desc');
    const filled: SessionRow = {
      session_id: 'a', started_utc: null, duration_min: 1, model: 'm',
      project: 'p', project_key: null, cost_usd: 0, cache_hit_pct: 94,
    };
    const empty: SessionRow = { ...filled, session_id: 'b', cache_hit_pct: null };
    // nullKey returns null for the empty row → applyTableSort parks it last.
    expect(cache.nullKey!(empty)).toBeNull();
    expect(cache.nullKey!(filled)).toBe(94);
    // comparator only sees non-null values.
    const lower: SessionRow = { ...filled, session_id: 'c', cache_hit_pct: 10 };
    expect(cache.compare(lower, filled)).toBeLessThan(0);
  });
});
