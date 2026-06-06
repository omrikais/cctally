// projectedContextText — metric-aware projected-axis context string.
//
// The `metric` discriminator selects the projected wording (weekly_pct vs
// budget_usd vs codex_budget_usd). Values are read FROM THE ROW
// (`context.projected_value` / `context.denominator`), never live config
// (Codex P0-4). The codex_budget_usd branch (#135 / Q5) tags the Codex
// vendor onto the budget-$ wording.
import { describe, expect, it } from 'vitest';
import { projectedContextText } from './alertAxis';
import type { AlertEntry } from '../types/envelope';

function projectedAlert(overrides: Partial<AlertEntry>): AlertEntry {
  return {
    id: 'projected:2026-06-01T00:00:00Z:90',
    axis: 'projected',
    threshold: 90,
    crossed_at: '2026-06-05T00:00:00Z',
    alerted_at: '2026-06-05T00:00:00Z',
    context: {},
    ...overrides,
  } as AlertEntry;
}

describe('projectedContextText', () => {
  it('renders the weekly_pct projection against the cap', () => {
    const alert = projectedAlert({
      metric: 'weekly_pct',
      context: { metric: 'weekly_pct', projected_value: 102, denominator: 100 },
    });
    expect(projectedContextText(alert)).toBe('projected 102% of cap');
  });

  it('renders the budget_usd projection against the target', () => {
    const alert = projectedAlert({
      metric: 'budget_usd',
      context: { metric: 'budget_usd', projected_value: 312, denominator: 300 },
    });
    expect(projectedContextText(alert)).toBe('projected $312 of $300');
  });

  it('renders the codex_budget_usd projection with the Codex vendor tag', () => {
    const alert = projectedAlert({
      metric: 'codex_budget_usd',
      context: {
        metric: 'codex_budget_usd',
        projected_value: 230,
        denominator: 200,
      },
    });
    expect(projectedContextText(alert)).toBe('projected $230 of $200 · Codex');
  });

  it('returns null for a codex_budget_usd row missing the denominator', () => {
    const alert = projectedAlert({
      metric: 'codex_budget_usd',
      context: { metric: 'codex_budget_usd', projected_value: 230 },
    });
    expect(projectedContextText(alert)).toBeNull();
  });

  it('returns null for a non-projected axis', () => {
    const alert = projectedAlert({ axis: 'budget' });
    expect(projectedContextText(alert)).toBeNull();
  });
});
