import { describe, expect, it } from 'vitest';
import { parseHash, formatHash, permalinkUrl } from './urlRouting';

describe('parseHash', () => {
  it('parses the qualified source/key route without decoding the opaque key', () => {
    expect(parseHash('#/conversations/source/codex/v1.root%2Fopaque/turn%201')).toMatchObject({
      conversationRef: { source: 'codex', key: 'v1.root/opaque' },
      turnUuid: 'turn 1',
      compare: null,
    });
  });

  it('round-trips a qualified comparison without colliding equal opaque keys', () => {
    const a = { source: 'claude', key: 'same/native' } as const;
    const b = { source: 'codex', key: 'same/native' } as const;
    const hash = formatHash({ sessionId: null, conversationRef: null, turnUuid: null, compare: { a, b } });
    expect(hash).toBe('#/conversations/compare/claude/same%2Fnative/codex/same%2Fnative');
    expect(parseHash(hash)?.compare).toEqual({ a, b });
  });

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
      sessionId: null, conversationRef: null, turnUuid: null,
      compare: { a: { source: 'claude', key: 'AA' }, b: { source: 'claude', key: 'BB' } },
    });
  });

  it('a single-session hash has compare === null', () => {
    expect(parseHash('#/conversations/s1')).toEqual({ sessionId: 's1', turnUuid: null, compare: null });
  });

  // #228 S3 F4 — the singular `#/conversation/<id>` form the issue literally
  // writes is a read-tolerance ALIAS of the canonical plural route.
  it('F4: accepts the singular /conversation/<id> as an alias of the plural route', () => {
    expect(parseHash('#/conversation/abc')).toEqual({ sessionId: 'abc', turnUuid: null, compare: null });
    expect(parseHash('#/conversation/abc/u1')).toEqual({ sessionId: 'abc', turnUuid: 'u1', compare: null });
  });

  it('F4: the singular alias also covers the no-selection + compare arms', () => {
    expect(parseHash('#/conversation')).toEqual({ sessionId: null, turnUuid: null, compare: null });
    expect(parseHash('#/conversation/')).toEqual({ sessionId: null, turnUuid: null, compare: null });
    expect(parseHash('#/conversation/compare/AA/BB')).toEqual({
      sessionId: null, conversationRef: null, turnUuid: null,
      compare: { a: { source: 'claude', key: 'AA' }, b: { source: 'claude', key: 'BB' } },
    });
  });

  it('F4: the singular alias must be a FULL segment (no false prefix match)', () => {
    expect(parseHash('#/conversationfoo')).toBeNull();
    expect(parseHash('#/conversationsfoo')).toBeNull();
  });
});

describe('formatHash', () => {
  it('writes qualified source plus opaque key and round-trips colliding identities', () => {
    const claude = { source: 'claude', key: 'same/native' } as const;
    const codex = { source: 'codex', key: 'same/native' } as const;
    const claudeHash = formatHash(claude as never, 'turn 1');
    const codexHash = formatHash(codex as never, 'turn 1');
    expect(claudeHash).toBe('#/conversations/source/claude/same%2Fnative/turn%201');
    expect(codexHash).toBe('#/conversations/source/codex/same%2Fnative/turn%201');
    expect(claudeHash).not.toBe(codexHash);
  });

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
    const h = formatHash({
      sessionId: null,
      conversationRef: null,
      turnUuid: null,
      compare: {
        a: { source: 'claude', key: 'a/x' },
        b: { source: 'claude', key: 'b x' },
      },
    });
    expect(h).toBe('#/conversations/compare/claude/a%2Fx/claude/b%20x');
    expect(parseHash(h)?.compare).toEqual({
      a: { source: 'claude', key: 'a/x' },
      b: { source: 'claude', key: 'b x' },
    });
  });
});

describe('permalinkUrl', () => {
  it('builds an absolute origin+pathname+hash URL', () => {
    expect(permalinkUrl('http://localhost', '/', 'abc', 'u1')).toBe(
      'http://localhost/#/conversations/abc/u1',
    );
  });
});
