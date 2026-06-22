import { describe, expect, it } from 'vitest';
import { domainOf, isHttpUrl } from './webUrl';

// #217 S6 U8 — dedicated coverage for the webUrl pure helpers. These were
// previously exercised only incidentally inside WebFetchCard.test.tsx; this
// gives domainOf / isHttpUrl a direct unit so a regression in either is caught
// at its source rather than via a card render.
describe('webUrl pure helpers (#217 S6 U8)', () => {
  it('domainOf returns the hostname, empty on garbage', () => {
    expect(domainOf('https://ccusage.com/guide/')).toBe('ccusage.com');
    expect(domainOf('https://sub.example.co.uk:8080/x?y=1')).toBe('sub.example.co.uk');
    expect(domainOf('not a url')).toBe('');
    expect(domainOf('')).toBe('');
  });
  it('isHttpUrl accepts only http(s)', () => {
    expect(isHttpUrl('https://x.com')).toBe(true);
    expect(isHttpUrl('HTTP://x.com')).toBe(true);
    expect(isHttpUrl('javascript:alert(1)')).toBe(false);
    expect(isHttpUrl('ftp://x.com')).toBe(false);
    expect(isHttpUrl('/relative')).toBe(false);
  });
});
