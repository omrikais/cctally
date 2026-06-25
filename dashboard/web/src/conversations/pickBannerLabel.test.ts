import { describe, expect, it } from 'vitest';
import { pickBannerLabel } from './pickBannerLabel';

describe('pickBannerLabel', () => {
  it('returns the cached title when present', () => {
    expect(pickBannerLabel('abcd1234ef', { abcd1234ef: 'Implement record-credit M2' }))
      .toEqual({ kind: 'title', text: 'Implement record-credit M2' });
  });
  it('truncates a long title with an ellipsis', () => {
    const long = 'x'.repeat(80);
    const out = pickBannerLabel('a', { a: long });
    expect(out.kind).toBe('title');
    expect(out.text.length).toBeLessThanOrEqual(48);
    expect(out.text.endsWith('…')).toBe(true);
  });
  it('falls back to the 8-char hash when the title is missing or blank', () => {
    expect(pickBannerLabel('abcd1234ef', {})).toEqual({ kind: 'hash', text: 'abcd1234' });
    expect(pickBannerLabel('abcd1234ef', { abcd1234ef: '   ' })).toEqual({ kind: 'hash', text: 'abcd1234' });
  });
});
