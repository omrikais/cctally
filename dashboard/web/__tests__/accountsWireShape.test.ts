// #341 Task 4 — WIRE-SHAPE GUARD for the conditional per-account wire (spec §4).
//
// Transcribed from the Python serializer (`bin/_cctally_dashboard_sources.py`
// build_codex_source_state / _codex_accounts_wire): the per-account decoration
// is emitted at `data.accounts[]` + `data.hero.cycles[]` ONLY when the provider
// has >1 REAL account. A <=1-real-account source must have NEITHER key (byte
// identity, R8). This guard fails loudly if the client fixtures/type ever drift
// so a unit test could validate a shape the server never produces.
import { describe, expect, it } from 'vitest';
import { sourceAccounts } from '../src/store/accountFocus';
import type { AccountCard, AccountHeroCycle, SourceEntry } from '../src/types/envelope';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

function undecoratedCodexEntry(): SourceEntry<unknown> {
  // No `accounts`, hero WITHOUT `cycles` — exactly today's byte shape.
  return {
    availability: 'ok', freshness: 'fresh', warnings: [], data_version: 'v',
    last_success_at: null, capabilities: {},
    data: { hero: { cost_usd: 1, cycle: null } },
  } as unknown as SourceEntry<unknown>;
}

function decoratedCodexEntry(): SourceEntry<unknown> {
  const accounts: AccountCard[] = [
    {
      accountKey: A, label: 'alice', plan: 'pro', active: true,
      weeklyPercent: 40, fiveHourPercent: 12, resetsAt: '2026-07-22T00:00:00+00:00',
      spendUsd: 1.5, inputTokens: 100, cachedInputTokens: 10, outputTokens: 20,
      reasoningOutputTokens: 5, totalTokens: 135,
    },
    {
      accountKey: 'unattributed', label: 'Unattributed', plan: null, active: false,
      weeklyPercent: null, fiveHourPercent: null, resetsAt: null, spendUsd: 0.2,
      inputTokens: 10, cachedInputTokens: 0, outputTokens: 0,
      reasoningOutputTokens: 0, totalTokens: 10, unattributed: true,
    },
  ];
  const cycles: AccountHeroCycle[] = [
    { accountKey: A, window_minutes: 10080, start_at: '2026-07-15T00:00:00+00:00',
      resets_at: '2026-07-22T00:00:00+00:00', used_percent: 40, cost_usd: 1.5, total_tokens: 135 },
  ];
  return {
    availability: 'ok', freshness: 'fresh', warnings: [], data_version: 'v',
    last_success_at: null, capabilities: {},
    data: { hero: { cost_usd: 1, cycle: null, cycles }, accounts },
  } as unknown as SourceEntry<unknown>;
}

describe('accounts[] wire shape (both conditional shapes, R8)', () => {
  it('a <=1-real-account source omits `accounts` AND `hero.cycles`', () => {
    const entry = undecoratedCodexEntry();
    const data = entry.data as { accounts?: unknown; hero: { cycles?: unknown } };
    expect(data.accounts).toBeUndefined();
    expect(data.hero.cycles).toBeUndefined();
    // The store helper reads it as "undecorated".
    expect(sourceAccounts(entry)).toBeNull();
  });

  it('a >1-real-account source carries `accounts[]` and `hero.cycles[]`', () => {
    const entry = decoratedCodexEntry();
    const data = entry.data as { accounts: AccountCard[]; hero: { cycles: AccountHeroCycle[] } };
    expect(Array.isArray(data.accounts)).toBe(true);
    expect(data.accounts.map((a) => a.accountKey)).toContain(A);
    // The unattributed bucket carries the dimming flag + null bars.
    const unattr = data.accounts.find((a) => a.accountKey === 'unattributed');
    expect(unattr?.unattributed).toBe(true);
    expect(unattr?.weeklyPercent).toBeNull();
    // Hero cycles: one per account with a live weekly cycle.
    expect(data.hero.cycles.map((c) => c.accountKey)).toEqual([A]);
    expect(sourceAccounts(entry)).not.toBeNull();
  });

  it('the guard rejects a bogus non-array `accounts`', () => {
    const entry = {
      availability: 'ok', freshness: 'fresh', warnings: [], data_version: 'v',
      last_success_at: null, capabilities: {}, data: { accounts: 'nope' },
    } as unknown as SourceEntry<unknown>;
    expect(sourceAccounts(entry)).toBeNull();
    void B;
  });
});
