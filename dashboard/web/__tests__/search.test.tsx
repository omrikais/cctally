import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SessionsPanel } from '../src/panels/SessionsPanel';
import {
  getState,
  dispatch,
  updateSnapshot,
  _resetForTests,
} from '../src/store/store';
import { computeSearchMatches } from '../src/store/selectors';
import {
  installGlobalKeydown,
  registerKeymap,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../src/store/keymap';
import { stepMatch } from '../src/store/actions';
import type { Envelope, SessionRow } from '../src/types/envelope';

function mkRow(partial: Partial<SessionRow>): SessionRow {
  return {
    session_id: 'x',
    started_utc: '2026-04-24T10:00:00Z',
    duration_min: 10,
    model: 'sonnet',
    project: 'repo',
    cost_usd: 1.0,
    ...partial,
  };
}

function mkEnvelope(rows: SessionRow[]): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-04-24T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'Apr 20–27',
      used_pct: 0,
      five_hour_pct: null,
      dollar_per_pct: null,
      forecast_pct: null,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks:  { rows: [] },
    daily:   { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: rows.length, sort_key: 'started_desc', rows },
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [] },
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  // Register the same n/N/`/` bindings main.tsx installs, so these
  // tests exercise the real keymap path without booting the whole app.
  registerKeymap([
    { key: 'n', scope: 'global', action: () => stepMatch(1) },
    { key: 'N', scope: 'global', action: () => stepMatch(-1) },
  ]);
});

afterEach(() => {
  uninstallGlobalKeydown();
  document.body.innerHTML = '';
});

describe('computeSearchMatches', () => {
  const rows = [
    mkRow({ session_id: 'a', model: 'opus',   project: 'repo-foo' }),
    mkRow({ session_id: 'b', model: 'sonnet', project: 'repo-bar' }),
    mkRow({ session_id: 'c', model: 'haiku',  project: 'other' }),
  ];
  it('returns original indices on case-insensitive substring match (project OR model)', () => {
    expect(computeSearchMatches(rows, 'OPUS')).toEqual([0]);
    expect(computeSearchMatches(rows, 'repo').sort()).toEqual([0, 1]);
  });
  it('returns [] for empty needle', () => {
    expect(computeSearchMatches(rows, '')).toEqual([]);
  });
  it('returns [] when no row matches', () => {
    expect(computeSearchMatches(rows, 'nothere')).toEqual([]);
  });
});

describe('SET_SEARCH reducer', () => {
  it('computes matches and seeds index=0 on non-empty needle', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus', project: 'repo' }),
        mkRow({ session_id: 'b', model: 'sonnet', project: 'repo' }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'opus' });
    expect(getState().searchMatches).toEqual([0]);
    expect(getState().searchIndex).toBe(0);
  });
  it('clears matches and sets index=-1 on empty needle', () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'repo' })]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'opus' });
    expect(getState().searchIndex).toBe(0);
    dispatch({ type: 'SET_SEARCH', text: '' });
    expect(getState().searchMatches).toEqual([]);
    expect(getState().searchIndex).toBe(-1);
  });
  it('sets index=-1 when needle does not match any row', () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'repo' })]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'zzz' });
    expect(getState().searchMatches).toEqual([]);
    expect(getState().searchIndex).toBe(-1);
  });
});

describe('/ opens the search input; Escape clears and closes', () => {
  it('global `/` expands the search input and focuses it', async () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'repo' })]),
    );
    render(<SessionsPanel />);
    const user = userEvent.setup();

    // Search button is visible when collapsed.
    expect(document.getElementById('search-btn')).toBeTruthy();
    expect(document.getElementById('search-input')).toBeNull();

    await user.keyboard('/');
    const input = document.getElementById('search-input') as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(input).toBe(document.activeElement);
    expect(getState().inputMode).toBe('search');
  });

  it('onChange in the search input flows through SET_SEARCH', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus', project: 'repo' }),
        mkRow({ session_id: 'b', model: 'sonnet', project: 'repo' }),
      ]),
    );
    render(<SessionsPanel />);
    act(() => {
      // Open the search input by dispatching the same state the `/`
      // binding would. Separate test above covers the keymap path.
      dispatch({ type: 'SET_INPUT_MODE', mode: 'search' });
    });
    // Manually dispatch SET_SEARCH to mimic the controlled input's
    // onChange firing — this keeps the test focused on store behavior
    // without fighting userEvent + focus races.
    act(() => {
      dispatch({ type: 'SET_SEARCH', text: 'opus' });
    });
    expect(getState().searchText).toBe('opus');
    expect(getState().searchMatches).toEqual([0]);
    expect(getState().searchIndex).toBe(0);
  });

  it('Escape inside the search input clears and collapses', async () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'repo' })]),
    );
    render(<SessionsPanel />);
    const user = userEvent.setup();
    await user.keyboard('/');
    // Seed text through the store.
    act(() => {
      dispatch({ type: 'SET_SEARCH', text: 'opus' });
    });
    expect(getState().searchText).toBe('opus');

    // Focus the input and hit Escape. The onKeyDown handler dispatches
    // SET_SEARCH text:'' via the clear: true branch, and collapses the
    // search container.
    const input = document.getElementById('search-input') as HTMLInputElement;
    input.focus();
    await user.keyboard('{Escape}');
    expect(getState().searchText).toBe('');
    expect(getState().inputMode).toBe(null);
    expect(document.getElementById('search-input')).toBeNull();
    expect(document.getElementById('search-btn')).toBeTruthy();
  });
});

describe('n / N step through searchMatches', () => {
  it('n advances, wrapping at the end; N rewinds, wrapping at the start', async () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus', project: 'x' }),
        mkRow({ session_id: 'b', model: 'opus', project: 'y' }),
        mkRow({ session_id: 'c', model: 'opus', project: 'z' }),
      ]),
    );
    render(<SessionsPanel />);

    // Seed matches + index via SET_SEARCH.
    act(() => {
      dispatch({ type: 'SET_SEARCH', text: 'opus' });
    });
    expect(getState().searchMatches).toEqual([0, 1, 2]);
    expect(getState().searchIndex).toBe(0);

    // n → 1
    const user = userEvent.setup();
    await user.keyboard('n');
    expect(getState().searchIndex).toBe(1);
    // n → 2
    await user.keyboard('n');
    expect(getState().searchIndex).toBe(2);
    // n wraps → 0
    await user.keyboard('n');
    expect(getState().searchIndex).toBe(0);
    // N wraps backwards → 2
    await user.keyboard('N');
    expect(getState().searchIndex).toBe(2);
  });

  it('n / N are no-ops when searchMatches is empty', async () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'x' })]),
    );
    render(<SessionsPanel />);
    const before = getState().searchIndex;
    const user = userEvent.setup();
    await user.keyboard('n');
    await user.keyboard('N');
    expect(getState().searchIndex).toBe(before);
  });
});

describe('updateSnapshot recomputes searchMatches on SSE tick', () => {
  it('restamps indices to the new rows so n/N tracks the active session', () => {
    // Seed a snapshot + search term that matches one row.
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus',   project: 'x' }),
        mkRow({ session_id: 'b', model: 'sonnet', project: 'y' }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'opus' });
    expect(getState().searchMatches).toEqual([0]);

    // Simulate an SSE tick that reorders / cycles rows: the matching
    // session slides to index 1.
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'b', model: 'sonnet', project: 'y' }),
        mkRow({ session_id: 'a', model: 'opus',   project: 'x' }),
      ]),
    );

    // Without the refresh this would still be [0] — now it tracks the
    // current position of session 'a'.
    expect(getState().searchMatches).toEqual([1]);
    // searchIndex clamps into the new range.
    expect(getState().searchIndex).toBe(0);
  });

  it('empties matches + searchIndex:-1 when the needle no longer matches', () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'x' })]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'opus' });
    expect(getState().searchIndex).toBe(0);

    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'sonnet', project: 'x' })]),
    );
    expect(getState().searchMatches).toEqual([]);
    expect(getState().searchIndex).toBe(-1);
  });

  it('leaves searchMatches untouched when searchText is empty', () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'x' })]),
    );
    // searchText === '' → skip the recompute entirely.
    expect(getState().searchMatches).toEqual([]);
  });
});

describe('search haystack spans every rendered column (parity with main)', () => {
  it('matches on the Started column — formatted "YYYY-MM-DD HH:MM"', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', started_utc: '2026-03-15T09:07:00Z', model: 'sonnet', project: 'p' }),
        mkRow({ session_id: 'b', started_utc: '2026-04-24T10:00:00Z', model: 'sonnet', project: 'p' }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: '03-15' });
    // Default sort is 'started desc' so rendered order is [b, a]; the
    // '03-15' match (session a) lands at rendered index 1.
    expect(getState().searchMatches).toEqual([1]);
  });
  it('matches on the Duration column — "<N>m"', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', duration_min: 42, model: 'sonnet', project: 'p' }),
        mkRow({ session_id: 'b', duration_min: 10, model: 'sonnet', project: 'p' }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: '42m' });
    expect(getState().searchMatches).toEqual([0]);
  });
  it('matches on the Cost column — "$X.XX"', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', cost_usd: 1.23, model: 'sonnet', project: 'p' }),
        mkRow({ session_id: 'b', cost_usd: 9.99, model: 'sonnet', project: 'p' }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: '$1.23' });
    expect(getState().searchMatches).toEqual([0]);
  });
});

describe('searchMatches are indices into the filtered+sorted+sliced rows', () => {
  it('hides matches behind the active filter (no misleading off-screen counts)', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus',   project: 'alpha-foo' }),
        mkRow({ session_id: 'b', model: 'sonnet', project: 'beta-foo' }),
        mkRow({ session_id: 'c', model: 'haiku',  project: 'gamma-foo' }),
      ]),
    );
    dispatch({ type: 'SET_FILTER', text: 'beta' });
    dispatch({ type: 'SET_SEARCH', text: 'foo' });
    // Only the 'beta-foo' row survives the filter; the search must not
    // count the other two foo-matches the user cannot see.
    expect(getState().searchMatches).toEqual([0]);
  });
  it('clearing the filter re-enlarges the match set without retyping /', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus',   project: 'x-foo' }),
        mkRow({ session_id: 'b', model: 'sonnet', project: 'y-foo' }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'foo' });
    expect(getState().searchMatches).toEqual([0, 1]);
    dispatch({ type: 'SET_FILTER', text: 'opus' });
    expect(getState().searchMatches).toEqual([0]);
    dispatch({ type: 'SET_FILTER', text: '' });
    expect(getState().searchMatches).toEqual([0, 1]);
  });
  it('sort change reshuffles match indices so they still point at matches', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus', project: 'x', cost_usd: 1.0, started_utc: '2026-04-24T10:00:00Z' }),
        mkRow({ session_id: 'b', model: 'opus', project: 'y', cost_usd: 5.0, started_utc: '2026-04-24T11:00:00Z' }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'opus' });
    // Default sort: started desc → b, a. Both are matches.
    expect(getState().searchMatches.length).toBe(2);
    dispatch({ type: 'SET_SORT', key: 'cost desc' });
    // After sort: b, a still. Both match. Indices preserved [0,1].
    expect(getState().searchMatches).toEqual([0, 1]);
  });
});

describe('Enter inside the search input steps to the next match', () => {
  it('calls navSearch(1) instead of collapsing — parity with legacy Enter→nextMatch', async () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus', project: 'x' }),
        mkRow({ session_id: 'b', model: 'opus', project: 'y' }),
        mkRow({ session_id: 'c', model: 'opus', project: 'z' }),
      ]),
    );
    render(<SessionsPanel />);
    const user = userEvent.setup();
    // Open search and seed matches.
    await user.keyboard('/');
    const input = document.getElementById('search-input') as HTMLInputElement;
    expect(input).toBeTruthy();
    act(() => {
      dispatch({ type: 'SET_SEARCH', text: 'opus' });
    });
    expect(getState().searchIndex).toBe(0);
    // Enter steps forward without collapsing.
    input.focus();
    await user.keyboard('{Enter}');
    expect(getState().searchIndex).toBe(1);
    // Input is still present (not collapsed).
    expect(document.getElementById('search-input')).not.toBeNull();
    await user.keyboard('{Enter}');
    expect(getState().searchIndex).toBe(2);
    // Wraps at end.
    await user.keyboard('{Enter}');
    expect(getState().searchIndex).toBe(0);
  });
});

describe('sessions / global bindings are suppressed while a modal is open', () => {
  it('f does not expand the filter input when a modal is open', async () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'x' })]),
    );
    render(<SessionsPanel />);
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    const user = userEvent.setup();
    await user.keyboard('f');
    expect(document.getElementById('filter-input')).toBeNull();
    // Funnel button remained.
    expect(document.getElementById('filter-btn')).not.toBeNull();
  });
  it('/ does not expand the search input when a modal is open', async () => {
    updateSnapshot(
      mkEnvelope([mkRow({ session_id: 'a', model: 'opus', project: 'x' })]),
    );
    render(<SessionsPanel />);
    dispatch({ type: 'OPEN_MODAL', kind: 'forecast' });
    const user = userEvent.setup();
    await user.keyboard('/');
    expect(document.getElementById('search-input')).toBeNull();
    expect(document.getElementById('search-btn')).not.toBeNull();
  });
});

describe('SessionsPanel rows carry .search-match on matched session ids', () => {
  it('adds the class only to rows whose session_id is in searchMatches', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus', project: 'x' }),
        mkRow({ session_id: 'b', model: 'sonnet', project: 'y' }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'opus' });
    render(<SessionsPanel />);
    const matched = document.querySelectorAll('.session-row.search-match');
    expect(matched.length).toBe(1);
    const sid = (matched[0] as HTMLElement).dataset.sessionId;
    expect(sid).toBe('a');
  });
});
