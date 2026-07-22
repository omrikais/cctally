// Beta-channel (spec 2026-07-21 §3): coerceUpdateState channel-awareness +
// selectConfiguredChannel. The coercer's install command must pin the EXACT
// resolved version on an npm beta channel (never bare @beta/@latest), mirroring
// the Python `_resolved_update_command`; stable/brew stay byte-identical.
import { describe, it, expect, beforeEach } from 'vitest';
import { coerceUpdateState } from './update';
import { dispatch, selectConfiguredChannel, _resetForTests } from './store';

const suppress = { skipped_versions: [], remind_after: null };

describe('coerceUpdateState — release channel', () => {
  const rawNpm = {
    current_version: '1.5.0',
    latest_version: '1.9.0',
    install: { method: 'npm' },
    check_status: 'ok',
  };

  it('defaults to stable + @latest when no channel is passed', () => {
    const s = coerceUpdateState(rawNpm, suppress)!;
    expect(s.configured_channel).toBe('stable');
    expect(s.update_command).toBe('npm install -g cctally@latest');
  });

  it('pins the exact resolved version on beta (never bare @beta/@latest)', () => {
    const s = coerceUpdateState(rawNpm, suppress, 'beta')!;
    expect(s.configured_channel).toBe('beta');
    expect(s.update_command).toBe('npm install -g cctally@1.9.0');
  });

  it('an unrecognized channel value falls back to stable', () => {
    const s = coerceUpdateState(rawNpm, suppress, 'nightly')!;
    expect(s.configured_channel).toBe('stable');
    expect(s.update_command).toBe('npm install -g cctally@latest');
  });

  it('brew stays the brew command even on beta', () => {
    const rawBrew = { ...rawNpm, install: { method: 'brew' } };
    const s = coerceUpdateState(rawBrew, suppress, 'beta')!;
    expect(s.configured_channel).toBe('beta');
    expect(s.update_command).toBe('brew update --quiet && brew upgrade cctally');
  });

  it('the _error sentinel still carries the channel', () => {
    const s = coerceUpdateState({ _error: 'boom' }, suppress, 'beta')!;
    expect(s.configured_channel).toBe('beta');
    expect(s.update_command).toBeNull();
  });
});

describe('selectConfiguredChannel', () => {
  beforeEach(() => { _resetForTests?.(); });

  it('defaults to stable when no update state exists', () => {
    expect(selectConfiguredChannel()).toBe('stable');
  });

  it('reflects the beta channel from the coerced update state', () => {
    const state = coerceUpdateState(
      { current_version: '1.5.0', latest_version: '1.9.0', install: { method: 'npm' } },
      suppress,
      'beta',
    );
    dispatch({ type: 'SET_UPDATE_STATE', state, suppress });
    expect(selectConfiguredChannel()).toBe('beta');
  });
});
