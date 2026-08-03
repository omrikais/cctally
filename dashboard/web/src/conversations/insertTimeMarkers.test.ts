import { describe, expect, it } from 'vitest';
import { insertTimeMarkers, type TimedNode } from './insertTimeMarkers';
import type { FilteredNode } from './applyFocusMode';
import type { ConversationItem } from '../types/conversation';
import type { FmtCtx } from '../lib/fmt';

const UTC: FmtCtx = { tz: 'Etc/UTC', offsetLabel: 'UTC' };
// #184 — June in New York is EDT (-04), not EST. `offsetLabel` is unused by
// insertTimeMarkers (it keys only on `tz` for the calendar-day boundary), so the
// label is cosmetic here; corrected to EDT to avoid a misleading fixture.
const NY: FmtCtx = { tz: 'America/New_York', offsetLabel: 'EDT' };

// A minimal `item` FilteredNode carrying a given ts.
function itemNode(uuid: string, ts: string | null): FilteredNode {
  const item = {
    kind: 'human',
    anchor: { session_id: 's', uuid, id: 0 },
    member_uuids: [uuid],
    ts: ts as never,
    text: uuid,
    blocks: [],
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
  } as ConversationItem;
  return { kind: 'item', item };
}

function hiddenRun(firstUuid: string, count = 2): FilteredNode {
  return { kind: 'hidden_run', count, firstUuid };
}

// Helper: indices/markers of the produced list.
function markers(out: TimedNode[]): Array<{ gapSeconds: number | null; dayLabel: string | null }> {
  return out
    .filter((n): n is Extract<TimedNode, { kind: 'time_marker' }> => n.kind === 'time_marker')
    .map((m) => ({ gapSeconds: m.gapSeconds, dayLabel: m.dayLabel }));
}

describe('insertTimeMarkers', () => {
  it('emits no marker when adjacent items are < 10 minutes apart', () => {
    const out = insertTimeMarkers(
      [itemNode('a', '2026-06-12T14:00:00Z'), itemNode('b', '2026-06-12T14:09:00Z')],
      UTC,
    );
    expect(markers(out)).toHaveLength(0);
    expect(out).toHaveLength(2);
  });

  it('emits a gap marker (with gapSeconds) when items are >= 10 minutes apart', () => {
    const out = insertTimeMarkers(
      [itemNode('a', '2026-06-12T14:00:00Z'), itemNode('b', '2026-06-12T14:42:00Z')],
      UTC,
    );
    const m = markers(out);
    expect(m).toHaveLength(1);
    expect(m[0].gapSeconds).toBe(42 * 60);
    expect(m[0].dayLabel).toBeNull();
    // Inserted BETWEEN the two items.
    expect(out.map((n) => n.kind)).toEqual(['item', 'time_marker', 'item']);
  });

  it('emits a marker at exactly 10 minutes (>= threshold, not >)', () => {
    const out = insertTimeMarkers(
      [itemNode('a', '2026-06-12T14:00:00Z'), itemNode('b', '2026-06-12T14:10:00Z')],
      UTC,
    );
    const m = markers(out);
    expect(m).toHaveLength(1);
    expect(m[0].gapSeconds).toBe(600);
  });

  it('emits a day-only marker on a calendar-day change without a 10-min gap', () => {
    // 23:55 → 00:01 next day = 6 min gap (< 10 min) but the UTC day flips.
    const out = insertTimeMarkers(
      [itemNode('a', '2026-06-12T23:55:00Z'), itemNode('b', '2026-06-13T00:01:00Z')],
      UTC,
    );
    const m = markers(out);
    expect(m).toHaveLength(1);
    expect(m[0].gapSeconds).toBeNull();
    expect(m[0].dayLabel).toBe('Jun 13');
  });

  it('emits a combined marker when BOTH a gap and a day change apply', () => {
    // 9.5h apart AND the day flips.
    const out = insertTimeMarkers(
      [itemNode('a', '2026-06-12T20:00:00Z'), itemNode('b', '2026-06-13T05:30:00Z')],
      UTC,
    );
    const m = markers(out);
    expect(m).toHaveLength(1);
    expect(m[0].gapSeconds).toBe(Math.round(9.5 * 3600));
    expect(m[0].dayLabel).toBe('Jun 13');
  });

  it('treats a null-ts item as transparent — no marker, and the chain spans it', () => {
    // a (14:00) → null-ts b → c (14:42). The 42-min gap is computed a→c, NOT a→b.
    const out = insertTimeMarkers(
      [
        itemNode('a', '2026-06-12T14:00:00Z'),
        itemNode('b', null),
        itemNode('c', '2026-06-12T14:42:00Z'),
      ],
      UTC,
    );
    const m = markers(out);
    expect(m).toHaveLength(1);
    expect(m[0].gapSeconds).toBe(42 * 60);
    // The marker sits before c (after the null-ts b), and no marker spans into b.
    const kinds = out.map((n) => n.kind);
    expect(kinds).toEqual(['item', 'item', 'time_marker', 'item']);
  });

  it('treats a hidden_run node as transparent — markers never span it', () => {
    const out = insertTimeMarkers(
      [
        itemNode('a', '2026-06-12T14:00:00Z'),
        hiddenRun('h'),
        itemNode('c', '2026-06-12T14:42:00Z'),
      ],
      UTC,
    );
    const m = markers(out);
    // Gap computed a→c across the hidden_run.
    expect(m).toHaveLength(1);
    expect(m[0].gapSeconds).toBe(42 * 60);
  });

  it('honors ctx.tz for the day boundary — the same instant pair flips between UTC and NY', () => {
    // 2026-06-13T03:30Z is still Jun 12 in America/New_York (EDT, -04 → 23:30
    // the prior day), so the pair straddles a UTC day boundary but NOT an NY one.
    const pair = [itemNode('a', '2026-06-12T23:00:00Z'), itemNode('b', '2026-06-13T03:30:00Z')];
    const utcM = markers(insertTimeMarkers(pair, UTC));
    const nyM = markers(insertTimeMarkers(pair, NY));
    // 4.5h gap → both emit a gap marker regardless, so compare the dayLabel.
    expect(utcM[0].dayLabel).toBe('Jun 13'); // UTC day changed
    expect(nyM[0].dayLabel).toBeNull();       // NY: both fall on Jun 12
  });

  it('emits nothing on an out-of-order (negative gap) pair', () => {
    const out = insertTimeMarkers(
      [itemNode('a', '2026-06-12T14:42:00Z'), itemNode('b', '2026-06-12T14:00:00Z')],
      UTC,
    );
    expect(markers(out)).toHaveLength(0);
  });

  it('returns an empty list for empty input', () => {
    expect(insertTimeMarkers([], UTC)).toEqual([]);
  });

  it('emits no marker before the first timestamped node (no prior anchor)', () => {
    const out = insertTimeMarkers(
      [itemNode('a', null), itemNode('b', '2026-06-12T14:00:00Z')],
      UTC,
    );
    expect(markers(out)).toHaveLength(0);
  });

  it('keeps a marker key stable when the same boundary shifts position (prepend) (#232)', () => {
    const ctx = { tz: 'UTC' } as any;
    const a = { kind: 'item', item: { anchor: { uuid: 'a' }, ts: '2026-06-24T00:00:00Z', member_uuids: ['a'] } } as any;
    const b = { kind: 'item', item: { anchor: { uuid: 'b' }, ts: '2026-06-24T01:00:00Z', member_uuids: ['b'] } } as any;
    const pre = { kind: 'item', item: { anchor: { uuid: 'p' }, ts: '2026-06-23T23:00:00Z', member_uuids: ['p'] } } as any;
    const before = insertTimeMarkers([a, b], ctx).find((n) => n.kind === 'time_marker') as any;
    const after = insertTimeMarkers([pre, a, b], ctx).filter((n) => n.kind === 'time_marker');
    const sameBoundary = after.find((n: any) => n.key === before.key);
    expect(sameBoundary).toBeTruthy(); // a→b marker keeps its key after prepending `pre`
  });

  it('keeps marker keys unique even when an out-of-order instant repeats (#184)', () => {
    // A non-monotonic transcript: the same instant recurs after a forward jump.
    // a (14:00) → b (14:42, +42m marker) → c (14:00 again, backwards: no marker)
    // → d (14:42 again, +42m marker). The two emitted markers share the SAME iso
    // ("…14:42:00Z"); folding the output position in keeps their keys distinct.
    const out = insertTimeMarkers(
      [
        itemNode('a', '2026-06-12T14:00:00Z'),
        itemNode('b', '2026-06-12T14:42:00Z'),
        itemNode('c', '2026-06-12T14:00:00Z'),
        itemNode('d', '2026-06-12T14:42:00Z'),
      ],
      UTC,
    );
    const keys = out
      .filter((n): n is Extract<TimedNode, { kind: 'time_marker' }> => n.kind === 'time_marker')
      .map((m) => m.key);
    expect(keys).toHaveLength(2);
    expect(new Set(keys).size).toBe(2); // unique despite the repeated instant
  });
});

// ── #463 S2 §4.2 — markers fire INSIDE a turn ───────────────────────────────

describe('#463 S2 intra-turn time markers', () => {
  // A segment carries a turn_uuid distinct from its own uuid when it is a
  // follower (segment_ordinal > 0). Markers key only on adjacent ITEM
  // timestamps, and since #463 S1 made the segment the item, two segments of
  // ONE turn are adjacent items and can therefore carry a marker between them.
  function segment(uuid: string, turn: string, ordinal: number, ts: string): FilteredNode {
    const item = {
      kind: 'assistant',
      anchor: { session_id: 's', uuid, id: 0 },
      member_uuids: [uuid], ts, text: uuid, blocks: [],
      is_sidechain: false, subagent_key: null, parent_uuid: null,
      turn_uuid: turn, segment_ordinal: ordinal, cost_usd: null,
    } as unknown as ConversationItem;
    return { kind: 'item', item };
  }

  it('inserts a time marker between two segments of ONE turn', () => {
    // 31 minutes, past the 600s threshold. Measured on a real store: 204 of the
    // 590 multi-segment turns hold an adjacent-segment gap over that threshold
    // and get a marker where before S1 they could not.
    const out = insertTimeMarkers([
      segment('s0', 't', 0, '2026-07-18T10:00:00Z'),
      segment('s1', 't', 1, '2026-07-18T10:31:00Z'),
    ], UTC);
    expect(out.map((n) => n.kind)).toEqual(['item', 'time_marker', 'item']);
    expect(markers(out)[0].gapSeconds).toBe(1860);
  });

  it('does not insert one when the intra-turn gap is under the threshold', () => {
    const out = insertTimeMarkers([
      segment('s0', 't', 0, '2026-07-18T10:00:00Z'),
      segment('s1', 't', 1, '2026-07-18T10:05:00Z'),
    ], UTC);
    expect(out.map((n) => n.kind)).toEqual(['item', 'item']);
  });

  it('cannot show a gap INSIDE one segment — the recorded residual', () => {
    // §4.2. `insertTimeMarkers` inspects ITEM timestamps only, so a gap between
    // two rows of the SAME segment is invisible. Measured: 54 segments across
    // 19 conversations hold an internal gap over the threshold — 0.41% of all
    // segments, worst case 27.8 hours. Fixing it would mean inserting markers
    // BETWEEN BLOCKS inside an item, changing the render structure for four
    // segments in a thousand. This test pins the limitation so a later reader
    // finds it stated rather than rediscovering it.
    //
    // The two halves together are what observe the property. A bare "one item in,
    // one node out" assertion would also hold for a function that returned its
    // input unchanged, so it could not see the limitation it names. The positive
    // control puts the SAME 27-hour span between two items and requires a marker;
    // the negative half puts it between two BLOCKS of one item and requires none.
    const early = '2026-07-18T10:00:00Z';
    const late = '2026-07-19T13:00:00Z';   // +27h — the measured worst case
    const across = insertTimeMarkers([
      segment('s0', 't', 0, early), segment('s1', 't', 1, late),
    ], UTC);
    expect(across.map((n) => n.kind)).toEqual(['item', 'time_marker', 'item']);
    expect(markers(across)[0].gapSeconds).toBe(27 * 3600);

    // One segment whose BLOCKS carry the same 27-hour span. `timestamp_utc` is
    // the per-row field the server holds; the neutral block type does not declare
    // it, which is itself the reason the client cannot act on it today.
    const inner = segment('s0', 't', 0, early) as { kind: 'item'; item: ConversationItem };
    inner.item.blocks = [
      { kind: 'text', text: 'first row', block_key: 'bk0', timestamp_utc: early },
      { kind: 'text', text: 'row 27 hours later', block_key: 'bk1', timestamp_utc: late },
    ] as unknown as ConversationItem['blocks'];
    const inside = insertTimeMarkers([inner], UTC);
    expect(inside.map((n) => n.kind)).toEqual(['item']);
    expect(markers(inside)).toHaveLength(0);
  });
});
