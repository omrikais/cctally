import { describe, expect, it } from 'vitest';
import {
  accountCard,
  makeAllSourceEntry,
  makeCodexSourceEntry,
  makeDecoratedCodexSourceData,
} from './sourceEnvelope';

describe('source envelope fixture fidelity', () => {
  it('preserves additive account-card wire fields supplied by an override', () => {
    const card = accountCard({
      accountKey: 'a'.repeat(32),
      cycleFreshness: 'stale',
    });

    expect(card.cycleFreshness).toBe('stale');
  });

  it('withholds combined totals when a provider is decorated', () => {
    const codex = makeCodexSourceEntry({ data: makeDecoratedCodexSourceData() });
    const all = makeAllSourceEntry(undefined, codex);

    expect(all.data?.combined).toBeNull();
    expect(all.data?.combined_unavailable).toEqual({
      code: 'multi_account_unsupported',
      message: 'Codex has 3 accounts on separate cycles, so a combined total '
        + 'is not published; see the per-account cards.',
      causes: [{
        provider: 'codex',
        code: 'multi_account_unsupported',
        detail: { account_count: 3 },
      }],
    });
  });
});
