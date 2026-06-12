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
