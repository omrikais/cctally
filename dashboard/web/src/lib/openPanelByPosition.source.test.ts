import { beforeEach, describe, expect, it } from 'vitest';
import { openPanelByPosition } from './openPanelByPosition';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import fixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope } from '../types/envelope';
import {
  ACCOUNT_B,
  makeDecoratedCodexSourceData,
} from '../test-utils/sourceEnvelope';

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

  it('Codex: position 1 opens the first session visible under account focus', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.codex.data = makeDecoratedCodexSourceData();
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: ACCOUNT_B });

    openPanelByPosition(1);

    expect(getState().openSourceDetail).toMatchObject({
      source: 'codex',
      resource: 'session',
      key: 'session:B',
    });
  });

  it('Codex: position 8 opens the active block visible under account focus', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    const codex = makeDecoratedCodexSourceData();
    codex.account_scopes![ACCOUNT_B].quota.blocks = [{
      ...codex.quota.blocks[0],
      key: 'block:B',
      account_key: ACCOUNT_B,
      is_active: true,
    }];
    env.sources!.codex.data = codex;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: ACCOUNT_B });

    openPanelByPosition(8);

    expect(getState().openSourceDetail).toMatchObject({
      source: 'codex',
      resource: 'block',
      key: 'block:B',
    });
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
      // #556 S2 §6.4 — the blocks list interleaves chronologically now, and
      // the "open the active block" affordance takes the FIRST DISPLAYED
      // active row. The fixture's Codex five-hour window starts 13:00, so the
      // Claude block under test starts later than it: this case is about
      // ROUTING a Claude-backed row through the canonical modal, and it must
      // not silently become a test of which provider happens to sort first.
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.claude.data!.quota.blocks = [{
      key: 'opaque:server-issued-block-key',
      source: 'claude',
      start_at: '2026-04-24T18:00:00Z',
      end_at: '2026-04-24T23:00:00Z',
      anchor: 'recorded',
      is_active: true,
      cost_usd: 4.2,
      models: [],
      label: '18:00 Apr 24 UTC',
    }];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });

    openPanelByPosition(8);

    expect(getState().openModal).toBe('block');
    expect(getState().openBlockStartAt).toBe('2026-04-24T18:00:00Z');
    expect(getState().openSourceDetail).toBeNull();
  });

  it('Claude: gating does not apply — position 1 still opens the most recent session', () => {
    openPanelByPosition(1);
    expect(getState().openModal).toBe('session');
  });
});
