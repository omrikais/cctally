import { beforeEach, describe, expect, it } from 'vitest';
import { buildGlobalKeyBindings, cycleActiveSource } from './globalBindings';
import { _resetForTests, dispatch, getState } from './store';
import { HELP_ROWS } from '../components/HelpOverlay';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function vBinding() {
  const b = buildGlobalKeyBindings().find((x) => x.key === 'v');
  if (!b) throw new Error('no v binding');
  return b;
}

describe("global `v` — cycle source (§5.4)", () => {
  it('cycles claude → codex → all → claude', () => {
    expect(getState().activeSource).toBe('claude');
    cycleActiveSource();
    expect(getState().activeSource).toBe('codex');
    cycleActiveSource();
    expect(getState().activeSource).toBe('all');
    cycleActiveSource();
    expect(getState().activeSource).toBe('claude');
  });

  it('the `v` binding drives the same cycle via its action', () => {
    const b = vBinding();
    b.action();
    expect(getState().activeSource).toBe('codex');
  });

  it('is guarded by the modal/input guard (inert while a panel modal is open)', () => {
    const b = vBinding();
    expect(b.when?.()).toBe(true);
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    expect(b.when?.()).toBe(false);
  });

  it('is a global-scope (dashboard-default) binding, not conversations-scoped', () => {
    const b = vBinding();
    expect(b.scope).toBe('global');
    expect(b.view).not.toBe('conversations');
  });

  it('HELP_ROWS documents the v cycle', () => {
    const row = HELP_ROWS.find((r) => r.keys.includes('v'));
    expect(row).toBeDefined();
    expect(row?.desc.toLowerCase()).toContain('cycle source');
  });
});
