import { describe, it, expect } from 'vitest';
import { computeAutoZoomDomain } from './chartDomain';

const BAND = 5;

describe('computeAutoZoomDomain', () => {
  it('zooms a high-clustered series off the 0 baseline (non-vacuous vs fixed 0-100)', () => {
    const pts = [97.2, 96.8, 98.1, 97.5, 96.2, 97.9, 98.4, 97.1, 95.8];
    const d = computeAutoZoomDomain(pts, 97.4, BAND);
    expect(d.lo).toBeGreaterThan(50); // fixed 0-100 would give lo=0
    expect(d.hi).toBeLessThanOrEqual(100);
    expect(d.lo).toBeLessThan(d.hi);
  });

  it('includes a low outlier day inside the domain (never clips it)', () => {
    const pts = [97.2, 96.8, 82.0, 97.5, 96.2];
    const d = computeAutoZoomDomain(pts, 96.5, BAND);
    expect(d.lo).toBeLessThanOrEqual(82.0);
    expect(d.hi).toBeGreaterThanOrEqual(97.5);
  });

  it('includes the full in-range +/- band around the median', () => {
    const d = computeAutoZoomDomain([97, 97.5, 96.5], 97, BAND);
    expect(d.lo).toBeLessThanOrEqual(97 - BAND); // 92
    // 97+5=102 clips at 100
    expect(d.hi).toBe(100);
  });

  it('enforces a minimum span on a dead-flat series', () => {
    const d = computeAutoZoomDomain([97, 97, 97], 97, BAND, { minSpan: 12, pad: 1 });
    expect(d.hi - d.lo).toBeGreaterThanOrEqual(12);
    expect(d.lo).toBeGreaterThanOrEqual(0);
    expect(d.hi).toBeLessThanOrEqual(100);
  });

  it('never leaves [0,100]', () => {
    const d = computeAutoZoomDomain([2, 1, 3], 2, BAND);
    expect(d.lo).toBeGreaterThanOrEqual(0);
    expect(d.hi).toBeLessThanOrEqual(100);
    expect(d.hi - d.lo).toBeGreaterThanOrEqual(12);
  });

  it('degenerates cleanly on empty points', () => {
    expect(computeAutoZoomDomain([], 97, BAND)).toEqual({ lo: 0, hi: 100 });
  });

  it('uses points only when median is null', () => {
    const d = computeAutoZoomDomain([90, 95], null, BAND);
    expect(d.lo).toBeLessThanOrEqual(90);
    expect(d.hi).toBeGreaterThanOrEqual(95);
  });
});
