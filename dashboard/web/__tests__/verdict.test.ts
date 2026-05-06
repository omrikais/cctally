import { describe, it, expect } from 'vitest';
import { VERDICT_MAP, resolveVerdict } from '../src/lib/verdict';

describe('VERDICT_MAP', () => {
  it('maps "capped" to OVER', () => {
    expect(VERDICT_MAP.capped).toEqual({
      label: 'OVER', cls: 'over', warn: true, accent: 'accent-red', glyph: '⛔',
    });
  });
  it('maps "cap" to WARN', () => {
    expect(VERDICT_MAP.cap).toEqual({
      label: 'WARN', cls: 'warn', warn: true, accent: 'accent-amber', glyph: '⚠',
    });
  });
  it('maps "ok" to OK', () => {
    expect(VERDICT_MAP.ok).toEqual({
      label: 'OK', cls: 'good', warn: false, accent: 'accent-green', glyph: '✓',
    });
  });
});

describe('resolveVerdict', () => {
  it('returns the matching entry for known verdicts', () => {
    expect(resolveVerdict('capped')?.label).toBe('OVER');
    expect(resolveVerdict('cap')?.label).toBe('WARN');
    expect(resolveVerdict('ok')?.label).toBe('OK');
  });
  it('returns null for unknown verdicts', () => {
    expect(resolveVerdict('unknown' as unknown as 'ok')).toBeNull();
    expect(resolveVerdict(null)).toBeNull();
    expect(resolveVerdict(undefined)).toBeNull();
  });
});
