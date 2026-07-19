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

// Digit shortcuts address the same canonical ten-card order in every mode.
describe('openPanelByPosition — source-visible addressing (§6.11)', () => {
  it('Claude: position 2 opens Trend', () => {
    openPanelByPosition(2);
    expect(getState().openModal).toBe('trend');
  });

  it('Codex: positions 8 and 9 address Blocks and Forecast', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    openPanelByPosition(8);
    expect(getState().openSourceDetail?.resource).toBe('block');
    dispatch({ type: 'CLOSE_SOURCE_DETAIL' });
    openPanelByPosition(9);
    expect(getState().openModal).toBe('forecast');
  });
});

describe('openPanelByPosition — source-bound non-Claude interactions', () => {
  it('Codex: position 2 opens the source-bound Trend modal', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    openPanelByPosition(2);
    expect(getState().openModal).toBe('trend');
    expect(getState().openModalSource).toBe('codex');
  });

  it('Codex: position 1 opens the qualified native session detail', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    openPanelByPosition(1);
    expect(getState().openModal).toBeNull();
    expect(getState().openSourceDetail).toMatchObject({ source: 'codex', resource: 'session' });
  });

  it('Codex: position 10 (Recent alerts) opens — canonical numbering is source-stable', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    openPanelByPosition(10);
    expect(getState().openModal).toBe('alerts');
  });

  it('All: position 1 opens one provider-qualified row from the chronological list', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    openPanelByPosition(1);
    expect(getState().openModal).toBeNull();
    expect(getState().openSourceDetail?.resource).toBe('session');
  });

  it('All: the Blocks position uses the canonical modal for a Claude-backed row', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.claude.data!.quota.blocks = [{
      key: 'opaque:server-issued-block-key',
      source: 'claude',
      start_at: '2026-04-24T08:00:00Z',
      end_at: '2026-04-24T13:00:00Z',
      anchor: 'recorded',
      is_active: true,
      cost_usd: 4.2,
      models: [],
      label: '08:00 Apr 24 UTC',
    }];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });

    openPanelByPosition(8);

    expect(getState().openModal).toBe('block');
    expect(getState().openBlockStartAt).toBe('2026-04-24T08:00:00Z');
    expect(getState().openSourceDetail).toBeNull();
  });

  it('Claude: gating does not apply — position 1 still opens the most recent session', () => {
    openPanelByPosition(1);
    expect(getState().openModal).toBe('session');
  });
});
