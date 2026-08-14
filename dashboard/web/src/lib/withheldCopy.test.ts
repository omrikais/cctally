import { describe, expect, it } from 'vitest';
import { withheldMessage } from './withheldCopy';

// #556 S2 §3.7 — every code has a rendering, INCLUDING one this build has
// never heard of. The panel must never render a silently empty table.
describe('withheldMessage', () => {
  it('names the missing fact for every known code', () => {
    expect(withheldMessage({ state: 'withheld', code: 'range_unresolved' }, 'ranking'))
      .toContain('shared range could not be resolved');
    expect(withheldMessage(
      { state: 'withheld', code: 'provider_unavailable', provider: 'codex' }, 'ranking',
    )).toContain('Codex data is unavailable');
    expect(withheldMessage(
      { state: 'withheld', code: 'provider_incoherent', provider: 'claude' }, 'ranking',
    )).toContain('Claude data is out of date');
    expect(withheldMessage({ state: 'withheld', code: 'claude_fold_failed' }, 'ranking'))
      .toContain("Claude's totals");
    expect(withheldMessage({ state: 'withheld', code: 'retained_range_mismatch' }, 'ranking'))
      .toContain('different ranges');
    expect(withheldMessage({ state: 'withheld', code: 'rows_absent' }, 'ranking'))
      .toContain('Reload');
  });

  it('renders generic copy for an unknown code from a newer server', () => {
    // The in-place update path deliberately lets an old client meet a newer
    // server without reloading the JavaScript, so this is an expected state.
    const message = withheldMessage(
      { state: 'withheld', code: 'some_future_code' }, 'history',
    );
    expect(message).toContain('withheld');
    expect(message).toContain('some_future_code');
  });

  it('takes the noun from the caller so one message serves both aggregates', () => {
    expect(withheldMessage({ state: 'withheld', code: 'range_unresolved' }, 'history'))
      .toContain('combined history');
  });

  it('never says a provider is at fault when the code names none', () => {
    expect(withheldMessage({ state: 'withheld', code: 'range_unresolved' }, 'ranking'))
      .not.toMatch(/Claude|Codex/);
  });
});
