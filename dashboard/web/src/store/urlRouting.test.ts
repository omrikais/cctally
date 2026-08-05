import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch } from './store';
import { installUrlRouting, runConversationEntry, parseHash, formatHash, permalinkUrl } from './urlRouting';

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

  it('treats a one-segment IdentityV1 key as provider-qualified', () => {
    const codex = 'v1.eyJ2ZXJzaW9uIjoxLCJzb3VyY2UiOiJjb2RleCIsInJlc291cmNlS2luZCI6ImNvbnZlcnNhdGlvbiIsIm5hdGl2ZUtleSI6IngifQ';
    const claude = 'v1.eyJ2ZXJzaW9uIjoxLCJzb3VyY2UiOiJjbGF1ZGUiLCJyZXNvdXJjZUtpbmQiOiJjb252ZXJzYXRpb24iLCJuYXRpdmVLZXkiOiJ5In0';
    expect(parseHash(`#/conversations/${codex}`)).toEqual({
      sessionId: null,
      conversationRef: { source: 'codex', key: codex },
      turnUuid: null,
      compare: null,
      qualified: true,
    });
    expect(parseHash(`#/conversations/${claude}`)).toMatchObject({
      sessionId: claude,
      conversationRef: { source: 'claude', key: claude },
      qualified: true,
    });
    expect(parseHash(`#/conversations/${codex}/turn-1`)).toMatchObject({
      conversationRef: { source: 'codex', key: codex },
      turnUuid: 'turn-1',
      qualified: true,
    });
    expect(parseHash('#/conversations/v1.bm90LWpzb24')).toEqual({
      sessionId: 'v1.bm90LWpzb24', turnUuid: null, compare: null,
    });
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

  it('pins account-qualified permalinks to the selected account', () => {
    const ref = { source: 'codex', key: 'v1.root-a', account_key: 'account-a' } as const;
    const hash = formatHash(ref, 'turn-1');
    expect(hash).toBe(
      '#/conversations/source/codex/v1.root-a/turn-1?account=account-a',
    );
    expect(parseHash(hash)?.conversationRef).toEqual(ref);
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

// #463 S5 (F24d) — a fake dispatch cannot see this. The store-to-URL reflector is
// active during a `hashchange`, and step 2 of the entry batch transiently clears
// the selected ref (SET_ACCOUNT_FOCUS, the #347 side effect), so the reflector
// observes that transition and pushes `#/conversations` before the target is
// pushed — leaving an entry the back button lands on.
//
// These tests drive the REAL singleton store, so they persist `activeSource` and
// `accountFocus` to localStorage as a side effect. Without the reset below, a
// second run inside one file (a retry, or simply the second test) would find the
// focus already set, SET_ACCOUNT_FOCUS would no-op, and the assertions would pass
// vacuously — which is exactly the failure this suite was rewritten to escape.
describe('#463 S5 — the entry batch does not pollute browser history', () => {
  function reset() {
    try { localStorage.clear(); } catch { /* jsdom always has it; be safe anyway */ }
    _resetForTests();
    window.history.replaceState(null, '', '/');
  }
  beforeEach(reset);
  afterEach(reset);

  // Record every pushState URL until `restore()` is called.
  function recordPushes(): { pushes: string[]; restore: () => void } {
    const pushes: string[] = [];
    const realPush = window.history.pushState.bind(window.history);
    window.history.pushState = ((state: unknown, title: string, url?: string) => {
      if (typeof url === 'string') pushes.push(url);
      return realPush(state as never, title, url as never);
    }) as typeof window.history.pushState;
    return { pushes, restore: () => { window.history.pushState = realPush; } };
  }

  it('following a cross-source permalink adds no intermediate history entry', async () => {
    // Two conditions are required to exercise the defect at all, and BOTH are
    // easy to omit into a test that passes over it.
    //
    // (1) The store must hold a selection when the link is followed, or the
    //     transient clear is a null-to-null transition the reflector cannot see.
    // (2) The link must carry an account that DIFFERS from the current focus.
    //     `SET_ACCOUNT_FOCUS` no-ops on an unchanged value before it clears
    //     anything (store.ts), and the seeded focus is ALL_ACCOUNTS — so an
    //     accountless link performs no clear and pushes nothing intermediate.
    //     The history defect is therefore reachable only through a
    //     focus-CHANGING deep link, which is narrower than the spec's §4.4
    //     wording implies.
    window.history.replaceState(null, '', '#/conversations/source/claude/SID-A');
    const dispose = installUrlRouting();
    const { pushes, restore } = recordPushes();
    try {
      window.history.replaceState(null, '', '#/conversations/source/codex/v1.KEY?account=acct-1');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
      await Promise.resolve();
      expect(pushes, `pushed: ${JSON.stringify(pushes)}`).not.toContain('#/conversations');
      expect(pushes.length).toBeLessThanOrEqual(1);

      // Non-vacuity: the reflector must still be observing AFTER the batch. A
      // suppression flag that is never cleared would satisfy every assertion
      // above while silently disabling the store-to-URL path for the rest of
      // the session, so prove one genuine post-batch change still writes.
      dispatch({ type: 'SELECT_CONVERSATION', sessionId: null });
      expect(pushes, `pushed: ${JSON.stringify(pushes)}`).toContain('#/conversations');
    } finally {
      restore();
      dispose();
    }
  });

  // #463 S5 (review F4) — the ComparisonView path. `ComparisonView`'s "open in
  // reader" resolves the board for the opened side, and it used to do so by
  // dispatching the entry batch straight into the real store while the reflector
  // was live. `SET_ACCOUNT_FOCUS` then set `compare: null` and
  // `selectedConversationRef: null` together, so the reflector's
  // `prev.cmp && !curr.cmp` branch pushed `#/conversations` before the open
  // pushed the target — the same artificial entry as above, on a path the hash
  // reader's suppression never reached. `runConversationEntry` is now the only
  // way in (`conversationEntryActions` is no longer exported), so the defect is
  // unreachable rather than merely unused.
  it('opening a comparison side under a different account adds no intermediate entry', () => {
    const a = { source: 'claude' as const, key: 'SID-A' };
    const b = { source: 'codex' as const, key: 'v1.KEY', account_key: 'acct-1' };
    window.history.replaceState(null, '', '#/conversations/source/claude/SID-A');
    const dispose = installUrlRouting();
    // Enter the comparison first: the defect needs `prev.cmp` set when the entry
    // batch clears `compare`.
    dispatch({ type: 'OPEN_COMPARE', aRef: a, bRef: b });
    expect(window.location.hash).toContain('/compare/');
    const { pushes, restore } = recordPushes();
    try {
      runConversationEntry(dispatch, b, {
        type: 'OPEN_CONVERSATION',
        conversationRef: b,
        jump: { conversation_ref: b, session_id: b.key, uuid: 'u9' },
      });

      expect(pushes, `pushed: ${JSON.stringify(pushes)}`).not.toContain('#/conversations');
      // And the address bar actually followed the open rather than stranding on
      // the comparison route — one entry, naming the opened side.
      expect(pushes, `pushed: ${JSON.stringify(pushes)}`).toHaveLength(1);
      expect(pushes[0]).toContain('/source/codex/v1.KEY');
    } finally {
      restore();
      dispose();
    }
  });
});
