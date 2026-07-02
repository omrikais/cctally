import { describe, it, expect } from 'vitest';
import { syncFreshness, humanizeAge, SYNC_AGING_S, SYNC_STALE_S } from './syncFreshness';

describe('humanizeAge', () => {
  it('formats seconds under a minute', () => {
    expect(humanizeAge(0)).toBe('0s ago');
    expect(humanizeAge(30)).toBe('30s ago');
    expect(humanizeAge(59)).toBe('59s ago');
  });
  it('formats minutes under an hour', () => {
    expect(humanizeAge(60)).toBe('1m ago');
    expect(humanizeAge(300)).toBe('5m ago');
    expect(humanizeAge(3599)).toBe('59m ago');
  });
  it('formats hours and minutes', () => {
    expect(humanizeAge(3600)).toBe('1h ago');       // exact hour drops 0m
    expect(humanizeAge(3720)).toBe('1h 2m ago');
    expect(humanizeAge(7260)).toBe('2h 1m ago');
    expect(humanizeAge(86399)).toBe('23h 59m ago'); // last second before the days tier
  });
  it('formats days and hours (#259 — >24h idle surfaces)', () => {
    expect(humanizeAge(86400)).toBe('1d ago');       // exact day drops 0h
    expect(humanizeAge(90000)).toBe('1d 1h ago');    // 25h
    expect(humanizeAge(97928)).toBe('1d 3h ago');    // ~27h — the reported case
    expect(humanizeAge(266400)).toBe('3d 2h ago');   // minutes elided at this magnitude
  });
  it('clamps negative / NaN to 0s', () => {
    expect(humanizeAge(-5)).toBe('0s ago');
    expect(humanizeAge(NaN)).toBe('0s ago');
  });
});

describe('syncFreshness bucket boundaries', () => {
  it('is fresh below the aging threshold', () => {
    expect(syncFreshness(0).bucket).toBe('fresh');
    expect(syncFreshness(SYNC_AGING_S - 1).bucket).toBe('fresh');   // 299s
  });
  it('is aging at [aging, stale)', () => {
    expect(syncFreshness(SYNC_AGING_S).bucket).toBe('aging');       // 300s
    expect(syncFreshness(SYNC_STALE_S - 1).bucket).toBe('aging');   // 1799s
  });
  it('is stale at/after the stale threshold', () => {
    expect(syncFreshness(SYNC_STALE_S).bucket).toBe('stale');       // 1800s
    expect(syncFreshness(99999).bucket).toBe('stale');
  });
  it('clamps negative to fresh and carries humanized text', () => {
    expect(syncFreshness(-1).bucket).toBe('fresh');
    expect(syncFreshness(480)).toEqual({ text: '8m ago', bucket: 'aging' });
  });
});
