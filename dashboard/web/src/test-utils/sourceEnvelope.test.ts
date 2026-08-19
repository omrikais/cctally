import { describe, expect, it } from 'vitest';
import type { AccountCard } from '../types/envelope';
import {
  accountCard,
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeDecoratedClaudeSourceData,
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

  it('publishes a certified account-cycle leg when a provider is decorated', () => {
    const codex = makeCodexSourceEntry({ data: makeDecoratedCodexSourceData() });
    const all = makeAllSourceEntry(undefined, codex);

    expect(all.data?.combined?.legs.codex.scope).toBe('account_cycles');
    expect(all.data?.combined?.legs.codex.accounts?.map((row) => row.account_key)).toEqual([
      'a'.repeat(32), 'b'.repeat(32), 'e'.repeat(32),
    ]);
    expect(all.data?.combined?.legs.codex.cost_usd).toBeCloseTo(12.3, 9);
    expect(all.data?.combined?.total_tokens).toBeNull();
    expect(all.data?.combined_unavailable).toBeUndefined();
  });

  it('preserves Claude subscription-week account periods in the synthesized certificate', () => {
    const data = makeDecoratedClaudeSourceData();
    data.accounts = data.accounts?.map((card, index) => ({
      ...card,
      spendWindow: {
        kind: 'subscription-week',
        startAt: `2026-04-${20 + index}T00:00:00Z`,
        endAt: `2026-04-${27 + index}T00:00:00Z`,
      },
    }));
    const all = makeAllSourceEntry(makeClaudeSourceEntry({ data }));

    expect(all.data?.combined?.legs.claude.accounts?.map((row) => row.period.kind))
      .toEqual(['subscription_week', 'subscription_week']);
    expect(all.data?.combined?.legs.claude.accounts?.map((row) => row.period.start_at))
      .toEqual(['2026-04-20T00:00:00Z', '2026-04-21T00:00:00Z']);
  });

  it('withholds instead of falling back when a decorated card lacks a period', () => {
    const data = makeDecoratedCodexSourceData();
    data.hero.cycles = data.hero.cycles?.filter(
      (cycle) => cycle.accountKey !== 'e'.repeat(32),
    );
    data.accounts = data.accounts?.map(({ spendWindow: _window, ...card }) => card);
    const all = makeAllSourceEntry(undefined, makeCodexSourceEntry({ data }));

    expect(all.data?.combined).toBeNull();
    expect(all.data?.combined_unavailable?.code).toBe('account_cycle_unresolved');
  });

  it('withholds instead of falling back when a decorated card cost is missing', () => {
    const data = makeDecoratedCodexSourceData();
    delete (data.accounts![0] as Partial<AccountCard>).spendUsd;
    const all = makeAllSourceEntry(undefined, makeCodexSourceEntry({ data }));

    expect(all.data?.combined).toBeNull();
    expect(all.data?.combined_unavailable?.code).toBe('account_cost_unresolved');
  });
});
