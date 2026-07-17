import { beforeEach, describe, expect, it } from 'vitest';
import { openPanelByPosition } from './openPanelByPosition';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import fixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  updateSnapshot(fixture as unknown as Envelope);
});

// §6.11 — digit shortcuts address VISIBLE positions only. The shared fixture's
// Claude view shows all 10 panels; the Codex view hides trend / cache-report /
// forecast (visible order: sessions, projects, daily, weekly, monthly, blocks,
// alerts).
describe('openPanelByPosition — source-visible addressing (§6.11)', () => {
  it('Claude: position 2 opens Trend', () => {
    openPanelByPosition(2);
    expect(getState().openModal).toBe('trend');
  });

  it('Codex: positions past the 7 visible panels are no-ops (hidden panels unreachable)', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    openPanelByPosition(8);
    expect(getState().openModal).toBeNull();
    openPanelByPosition(9);
    expect(getState().openModal).toBeNull();
  });
});

// ui-qa round-3 P2 — the legacy panel-family modals are Claude-shaped, so a
// digit under a non-Claude selection must NOT open one (it would render Claude
// data under the Codex/All label). Only source-aware modals (alerts) open.
describe('openPanelByPosition — non-Claude selections gate legacy modals', () => {
  it('Codex: position 2 (Projects, visible) is a no-op — legacy modal stays closed', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    openPanelByPosition(2);
    expect(getState().openModal).toBeNull();
  });

  it('Codex: position 1 (Sessions, visible) is a no-op — Claude session modal cannot open', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    openPanelByPosition(1);
    expect(getState().openModal).toBeNull();
    expect(getState().openSessionId ?? null).toBeNull();
  });

  it('Codex: position 7 (Recent alerts) opens — the alerts modal is source-aware', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    openPanelByPosition(7);
    expect(getState().openModal).toBe('alerts');
  });

  it('All: legacy panel modals do not open via digits either', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    openPanelByPosition(1);
    expect(getState().openModal).toBeNull();
  });

  it('Claude: gating does not apply — position 1 still opens the most recent session', () => {
    openPanelByPosition(1);
    expect(getState().openModal).toBe('session');
  });
});
