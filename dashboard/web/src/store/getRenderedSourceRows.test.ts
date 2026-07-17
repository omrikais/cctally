import { beforeEach, describe, expect, it } from 'vitest';
import {
  _resetForTests,
  dispatch,
  getRenderedSourceRows,
  getState,
  updateSnapshot,
} from './store';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

function bundleEnv(): Envelope {
  return makeSourceEnvelope() as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('getRenderedSourceRows (§6.3)', () => {
  it('Codex → native rows, default-sorted by last_activity desc, source preserved', () => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const rows = getRenderedSourceRows();
    // Session 1 last_activity 12:30 precedes Session 2 at 11:00.
    expect(rows.map((r) => r.key)).toEqual(['session:codex-a', 'session:codex-b']);
    expect(rows.every((r) => r.source === 'codex')).toBe(true);
    expect(rows[0].title).toBe('Session 1');
    expect(rows[0].recencyUtc).toBe('2026-04-24T12:30:00Z');
  });

  it('Claude → [] (Claude renders via the legacy getRenderedRows path)', () => {
    updateSnapshot(bundleEnv());
    expect(getRenderedSourceRows()).toEqual([]);
  });

  it('filter narrows by the label + models haystack', () => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    // gpt-5-codex is only Session 2's model.
    dispatch({ type: 'SET_FILTER', text: 'gpt-5-codex' });
    expect(getRenderedSourceRows().map((r) => r.key)).toEqual(['session:codex-b']);
    // A label match works too.
    dispatch({ type: 'SET_FILTER', text: 'Session 1' });
    expect(getRenderedSourceRows().map((r) => r.key)).toEqual(['session:codex-a']);
  });

  it('sort override by cost reorders (desc then asc)', () => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_SOURCE_SESSIONS_SORT', override: { column: 'cost', direction: 'desc' } });
    expect(getRenderedSourceRows().map((r) => r.key)).toEqual(['session:codex-a', 'session:codex-b']); // 6.4 > 5.9
    dispatch({ type: 'SET_SOURCE_SESSIONS_SORT', override: { column: 'cost', direction: 'asc' } });
    expect(getRenderedSourceRows().map((r) => r.key)).toEqual(['session:codex-b', 'session:codex-a']);
  });

  it('sort override by total tokens is available', () => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_SOURCE_SESSIONS_SORT', override: { column: 'total', direction: 'asc' } });
    // Both fixture sessions have total 276000; order stays stable but the sort resolves.
    expect(getRenderedSourceRows()).toHaveLength(2);
  });

  it('sort override by label uses the title', () => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_SOURCE_SESSIONS_SORT', override: { column: 'label', direction: 'asc' } });
    expect(getRenderedSourceRows().map((r) => r.title)).toEqual(['Session 1', 'Session 2']);
  });

  it('All → Claude + Codex rows interleaved by recency, each keeping its native source/key', () => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const rows = getRenderedSourceRows();
    // codex-a 12:30, codex-b 11:00, claude-a 10:00 → recency desc.
    expect(rows.map((r) => r.source)).toEqual(['codex', 'codex', 'claude']);
    expect(rows.map((r) => r.key)).toEqual([
      'session:codex-a',
      'session:codex-b',
      'session:claude-a',
    ]);
    // No merging: both providers are represented and labels stay native.
    expect(new Set(rows.map((r) => r.source))).toEqual(new Set(['claude', 'codex']));
    expect(rows.find((r) => r.source === 'claude')?.title).toBe('project-00');
  });

  // #294 S5 QA regression: the real server spreads `sources` at the envelope top
  // level, so under the phantom nested shape All-mode resolved entry=null and the
  // Sessions panel showed "(0 shown)" even with 100 Claude sessions present. With
  // the flat wire shape resolved, All-mode must surface the Claude provider rows
  // even when Codex is unavailable (providers.codex null).
  it('All → shows the Claude provider rows when ONLY Claude has data (Codex unavailable)', () => {
    const claude = makeClaudeSourceEntry();
    const codex = makeCodexSourceEntry({
      availability: 'unavailable',
      freshness: 'stale',
      data: null,
      capabilities: {},
      warnings: [{ code: 'source_build_failed', message: 'Source data could not be built.' }],
      last_success_at: null,
    });
    const all = makeAllSourceEntry(claude, codex);
    updateSnapshot(
      makeSourceEnvelope({ sources: { claude, codex, all } }) as unknown as Envelope,
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const rows = getRenderedSourceRows();
    expect(rows.map((r) => r.source)).toEqual(['claude']);
    expect(rows.map((r) => r.key)).toEqual(['session:claude-a']);
  });
});

describe('source search recompute (§6.3 — n/N alignment)', () => {
  it('Codex search matches label + models and indexes into rendered rows', () => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_SEARCH', text: 'Session 2' });
    expect(getState().searchMatches).toEqual([1]); // codex-b at rendered index 1
  });

  it('Claude search is unchanged (legacy row + haystack path)', () => {
    updateSnapshot({
      sessions: {
        total: 1,
        rows: [
          {
            session_id: 's1',
            project: 'alpha',
            project_key: 'alpha',
            model: 'claude-opus-4-8',
            started_utc: '2026-04-24T10:00:00Z',
            duration_min: 5,
            cost_usd: 1,
          },
        ],
      },
    } as unknown as Envelope);
    dispatch({ type: 'SET_SEARCH', text: 'alpha' });
    expect(getState().searchMatches).toEqual([0]);
  });

  it('switching source recomputes the active search over the new source rows', () => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_SEARCH', text: 'Session 1' });
    expect(getState().searchMatches).toEqual([0]);
    // Switching to All keeps the needle but recomputes over the interleaved rows;
    // 'Session 1' is the codex-a row, now at index 0 of the All interleave.
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    expect(getState().searchMatches).toEqual([0]);
  });
});
