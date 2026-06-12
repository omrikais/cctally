import { describe, it, expect } from 'vitest';
import { fmt } from './fmt';

describe('fmt.durationMs', () => {
  it('formats sub-minute as X.Xs', () => {
    expect(fmt.durationMs(10668)).toBe('10.7s');
    expect(fmt.durationMs(4200)).toBe('4.2s');
  });
  it('formats >= 60s as Xm Ys, dropping a trailing 0s', () => {
    expect(fmt.durationMs(125000)).toBe('2m 5s');
    expect(fmt.durationMs(120000)).toBe('2m');
  });
  it('carries 59.5s+ up to the next whole minute (no "Xm 60s")', () => {
    expect(fmt.durationMs(119999)).toBe('2m');
    expect(fmt.durationMs(179500)).toBe('3m');
    expect(fmt.durationMs(59999)).toBe('1m');
  });
  it('handles null/undefined', () => {
    expect(fmt.durationMs(null)).toBe('—');
    expect(fmt.durationMs(undefined)).toBe('—');
  });
});

describe('fmt.gapDuration (#177 S5)', () => {
  it('renders "—" for null/undefined/NaN/negative', () => {
    expect(fmt.gapDuration(null)).toBe('—');
    expect(fmt.gapDuration(undefined)).toBe('—');
    expect(fmt.gapDuration(NaN)).toBe('—');
    expect(fmt.gapDuration(-5)).toBe('—');
  });
  it('renders < 60 min as whole minutes', () => {
    expect(fmt.gapDuration(2520)).toBe('42 min');   // 42 min
    expect(fmt.gapDuration(600)).toBe('10 min');    // exactly the gap threshold
  });
  it('promotes to hours once rounded minutes hit 60 (no "60 min")', () => {
    // 3599s rounds to 60 min — must read "1 h", not "60 min".
    expect(fmt.gapDuration(3599)).toBe('1 h');
    // 3570s (59.5 min) also rounds to 60 min and must promote.
    expect(fmt.gapDuration(3570)).toBe('1 h');
  });
  it('renders >= 60 min as one-decimal hours, dropping a trailing .0', () => {
    expect(fmt.gapDuration(3600)).toBe('1 h');       // 1.0 -> "1"
    expect(fmt.gapDuration(7200)).toBe('2 h');       // 2.0 -> "2"
    expect(fmt.gapDuration(34200)).toBe('9.5 h');    // 9.5
  });
});

describe('fmt.tokens (#177 S5)', () => {
  it('renders "—" for null/undefined/NaN', () => {
    expect(fmt.tokens(null)).toBe('—');
    expect(fmt.tokens(undefined)).toBe('—');
    expect(fmt.tokens(NaN)).toBe('—');
  });
  it('renders < 1000 as a raw integer', () => {
    expect(fmt.tokens(873)).toBe('873');
    expect(fmt.tokens(0)).toBe('0');
  });
  it('renders >= 1000 as one-decimal k (trailing .0 dropped)', () => {
    expect(fmt.tokens(1200)).toBe('1.2k');
    expect(fmt.tokens(310000)).toBe('310k');
  });
  it('renders >= 1_000_000 as one-decimal M (trailing .0 dropped)', () => {
    expect(fmt.tokens(4_100_000)).toBe('4.1M');
    expect(fmt.tokens(2_000_000)).toBe('2M');
  });
});
