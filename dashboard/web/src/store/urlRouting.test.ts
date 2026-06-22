import { describe, expect, it } from 'vitest';
import { parseHash, formatHash, permalinkUrl } from './urlRouting';

describe('parseHash', () => {
  it('returns null for the dashboard (bare/empty/malformed)', () => {
    expect(parseHash('')).toBeNull();
    expect(parseHash('#')).toBeNull();
    expect(parseHash('#/')).toBeNull();
    expect(parseHash('#/dashboard')).toBeNull();
    expect(parseHash('#/conversationsfoo')).toBeNull(); // prefix must be a full segment
    expect(parseHash('#/conversations/s/u/extra')).toBeNull(); // 3+ segments
  });

  it('parses the no-selection conversations route', () => {
    expect(parseHash('#/conversations')).toEqual({ sessionId: null, turnUuid: null, compare: null });
    expect(parseHash('#/conversations/')).toEqual({ sessionId: null, turnUuid: null, compare: null });
  });

  it('parses a selected conversation and a turn', () => {
    expect(parseHash('#/conversations/abc')).toEqual({ sessionId: 'abc', turnUuid: null, compare: null });
    expect(parseHash('#/conversations/abc/u1')).toEqual({ sessionId: 'abc', turnUuid: 'u1', compare: null });
  });

  it('decodes percent-encoded segments', () => {
    expect(parseHash('#/conversations/a%2Fb/u%201')).toEqual({ sessionId: 'a/b', turnUuid: 'u 1', compare: null });
  });

  // #217 S7 F10 — the compare route.
  it('parses a compare hash', () => {
    expect(parseHash('#/conversations/compare/AA/BB')).toEqual({
      sessionId: null, turnUuid: null, compare: { a: 'AA', b: 'BB' },
    });
  });

  it('a single-session hash has compare === null', () => {
    expect(parseHash('#/conversations/s1')).toEqual({ sessionId: 's1', turnUuid: null, compare: null });
  });
});

describe('formatHash', () => {
  it('formats the four shapes and round-trips with parseHash', () => {
    expect(formatHash(null)).toBe('#/conversations');
    expect(formatHash('abc')).toBe('#/conversations/abc');
    expect(formatHash('abc', 'u1')).toBe('#/conversations/abc/u1');
    expect(parseHash(formatHash('abc', 'u1'))).toEqual({ sessionId: 'abc', turnUuid: 'u1', compare: null });
  });

  it('encodes unsafe characters but round-trips back to the raw value', () => {
    const h = formatHash('a/b', 'u 1');
    expect(h).toBe('#/conversations/a%2Fb/u%201');
    expect(parseHash(h)).toEqual({ sessionId: 'a/b', turnUuid: 'u 1', compare: null });
  });

  // #217 S7 F10 — formatHash also accepts a Route object (the write-back path),
  // so a compare route round-trips.
  it('round-trips a compare hash with encoding', () => {
    const h = formatHash({ sessionId: null, turnUuid: null, compare: { a: 'a/x', b: 'b x' } });
    expect(h).toBe('#/conversations/compare/a%2Fx/b%20x');
    expect(parseHash(h)).toEqual({ sessionId: null, turnUuid: null, compare: { a: 'a/x', b: 'b x' } });
  });
});

describe('permalinkUrl', () => {
  it('builds an absolute origin+pathname+hash URL', () => {
    expect(permalinkUrl('http://localhost', '/', 'abc', 'u1')).toBe(
      'http://localhost/#/conversations/abc/u1',
    );
  });
});
