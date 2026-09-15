// #769 S6 / #753 — the focused hero stops fabricating a spend.
//
// `focusedHero` read `card?.spendUsd ?? 0`. Beside a `SPENT THIS WEEK` label a
// zero is a claim: it says this account spent nothing. The server publishes the
// per-account hero operands and the card from one generation, so an absent card
// means there is no figure, and null is what renders that honestly. The
// aggregate's publication state is the provider's, so a focused account
// inherits `update_state` too.
import { describe, expect, it } from 'vitest';
import { scopeToAccount } from './accountScope';
import {
  ACCOUNT_A,
  ACCOUNT_B,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

function decoratedCodexEnv(
  mut?: (data: ReturnType<typeof makeDecoratedCodexSourceData>) => void,
): Envelope {
  const slice = makeSourceEnvelope();
  const data = makeDecoratedCodexSourceData();
  mut?.(data);
  slice.sources!.codex.data = data;
  return {
    header: {
      used_pct: 1, week_label: 'wk', five_hour_pct: null, dollar_per_pct: 1,
      forecast_pct: 1, forecast_verdict: 'ok', vs_last_week_delta: null,
    },
    current_week: null,
    ...slice,
  } as unknown as Envelope;
}

function focusedCodexHero(env: Envelope, accountKey: string) {
  const scoped = scopeToAccount(env, 'codex', accountKey);
  return scoped.data?.hero ?? null;
}

describe('focusedHero (#753)', () => {
  it('carries the focused card spend rather than a fabricated zero', () => {
    const env = decoratedCodexEnv((data) => {
      const card = data.accounts!.find((row) => row.accountKey === ACCOUNT_A)!;
      card.spendUsd = 33.75;
    });
    expect(focusedCodexHero(env, ACCOUNT_A)?.cost_usd).toBe(33.75);
  });

  it('never shows the merged provider figure under one account chip', () => {
    // The focused figure must be the card's own, never the aggregate hero's.
    // `focusedHero`'s absent-card branch is defensive rather than reachable:
    // `resolveAccountFocus` only resolves a key that appears in `accounts`, so
    // a resolved focus always has a card. It reads `?? null` rather than
    // `?? 0` regardless, because beside a `SPENT THIS WEEK` label a zero is a
    // claim that the account spent nothing.
    const env = decoratedCodexEnv((data) => {
      data.hero.cost_usd = 999;
      data.accounts!.find((row) => row.accountKey === ACCOUNT_A)!.spendUsd = 4;
      data.accounts!.find((row) => row.accountKey === ACCOUNT_B)!.spendUsd = 7;
    });
    expect(focusedCodexHero(env, ACCOUNT_A)?.cost_usd).toBe(4);
    expect(focusedCodexHero(env, ACCOUNT_B)?.cost_usd).toBe(7);
  });

  it('inherits the provider publication state onto a focused account', () => {
    const env = decoratedCodexEnv((data) => {
      data.hero.update_state = 'updating';
    });
    expect(focusedCodexHero(env, ACCOUNT_A)?.update_state).toBe('updating');
  });

  it('omits the marker entirely on a coherent generation', () => {
    const env = decoratedCodexEnv();
    expect(focusedCodexHero(env, ACCOUNT_A)?.update_state).toBeUndefined();
  });
});
