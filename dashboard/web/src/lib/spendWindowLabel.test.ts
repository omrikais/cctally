// #564 — the fallback window's copy is DERIVED from the published bounds, not
// hardcoded to seven days. The server clamps the window to the accounting range
// it actually loaded, so a hardcoded phrase would be false in exactly the case
// the clamp exists for. These cases pin the derivation rather than the phrase.
import { describe, expect, it } from 'vitest';
import { spendWindowLabel } from './fmt';

const KIND = 'trailing-cycle' as const;

describe('spendWindowLabel', () => {
  it('names a certified Claude period as a subscription week', () => {
    expect(spendWindowLabel({
      kind: 'subscription-week',
      startAt: '2026-04-20T00:00:00Z',
      endAt: '2026-04-27T00:00:00Z',
    })).toBe('subscription week');
  });

  it('names a full native cycle as the operator-chosen phrase', () => {
    expect(spendWindowLabel({
      kind: KIND,
      startAt: '2026-04-17T13:00:00Z',
      endAt: '2026-04-24T13:00:00Z',
    })).toBe('last 7 days');
  });

  it('names the true span when the accounting range clamped the window', () => {
    expect(spendWindowLabel({
      kind: KIND,
      startAt: '2026-04-21T13:00:00Z',
      endAt: '2026-04-24T13:00:00Z',
    })).toBe('last 3 days');
  });

  it('singularizes a one-day window', () => {
    expect(spendWindowLabel({
      kind: KIND,
      startAt: '2026-04-23T13:00:00Z',
      endAt: '2026-04-24T13:00:00Z',
    })).toBe('last 1 day');
  });

  // Review P3: rounding alone rendered `last 0 days` for a window shorter than
  // twelve hours, which claims a period covering nothing. A window that short
  // still covers at most one day of spend.
  it('never claims a zero-day period for a sub-day window', () => {
    expect(spendWindowLabel({
      kind: KIND,
      startAt: '2026-04-24T09:00:00Z',
      endAt: '2026-04-24T13:00:00Z',
    })).toBe('last 1 day');
  });

  // Unparseable bounds are a wire fault, not a period claim: the full-cycle
  // wording is the safe read because the server only ever bounds to that width.
  it('falls back to the full-cycle wording on unparseable bounds', () => {
    expect(spendWindowLabel({
      kind: KIND,
      startAt: 'not-a-date',
      endAt: '2026-04-24T13:00:00Z',
    })).toBe('last 7 days');
  });
});
