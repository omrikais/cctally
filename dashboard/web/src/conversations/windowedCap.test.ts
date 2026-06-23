import { describe, expect, it } from 'vitest';
import { planTrim } from './windowedCap';
import type { ConversationItem } from '../types/conversation';

// #228 S3 B3 (2b) — pure-helper tests for the windowed DOM cap. A minimal item
// factory: `anchor.id` is the edge-cursor value (the server's cache rowid), and
// `anchor.uuid` / `member_uuids` carry the uuid the protected-set is keyed on.
function mk(start: number, count: number): ConversationItem[] {
  const items: ConversationItem[] = [];
  for (let i = start; i < start + count; i++) {
    const uuid = `u${i}`;
    items.push({
      kind: 'human',
      anchor: { session_id: 's', uuid, id: i },
      member_uuids: [uuid],
    } as ConversationItem);
  }
  return items;
}
const NONE = new Set<string>();

describe('planTrim (#228 S3 B3 windowed DOM cap)', () => {
  it('drops the far BOTTOM edge after an over-cap prepend', () => {
    const items = mk(0, 1000); // u0..u999
    const r = planTrim({ items, op: 'prepend', cap: 600, protectedUuids: NONE, fetchInFlight: false });
    expect(r.droppedBottom).toBe(400);
    expect(r.droppedTop).toBe(0);
    expect(r.keep).toHaveLength(600);
    expect(r.keep[0].anchor.uuid).toBe('u0');          // top (just-prepended) kept
    expect(r.keep[599].anchor.uuid).toBe('u599');      // new bottom edge
    // The bottom cursor re-arms at the new last-kept item so scroll-down re-fetches.
    expect(r.resetBottomCursorTo).toBe(599);
    expect(r.resetTopCursorTo).toBeNull();
  });

  it('drops the far TOP edge after an over-cap append', () => {
    const r = planTrim({ items: mk(0, 1000), op: 'append', cap: 600, protectedUuids: NONE, fetchInFlight: false });
    expect(r.droppedTop).toBe(400);
    expect(r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(600);
    expect(r.keep[0].anchor.uuid).toBe('u400');        // new top edge
    expect(r.keep[599].anchor.uuid).toBe('u999');      // bottom (just-appended) kept
    // The top cursor re-arms at the new first-kept item so scroll-up re-fetches.
    expect(r.resetTopCursorTo).toBe(400);
    expect(r.resetBottomCursorTo).toBeNull();
  });

  it('does NOT trim into a page holding a protected uuid (prepend / bottom drop)', () => {
    // A protected uuid lives near the bottom (u950). The bottom-trim must STOP
    // before dropping it — trimming less that round.
    const r = planTrim({ items: mk(0, 1000), op: 'prepend', cap: 600, protectedUuids: new Set(['u950']), fetchInFlight: false });
    expect(r.droppedBottom).toBeLessThan(400);         // stopped short of the protected uuid
    expect(r.keep.some((it) => it.anchor.uuid === 'u950')).toBe(true);
    expect(r.keep[r.keep.length - 1].anchor.uuid).toBe('u950');
    // 1000 items, keep through u950 inclusive ⇒ 951 kept, 49 dropped.
    expect(r.keep).toHaveLength(951);
    expect(r.droppedBottom).toBe(49);
    expect(r.resetBottomCursorTo).toBe(950);
  });

  it('does NOT trim into a page holding a protected uuid (append / top drop)', () => {
    // A protected uuid near the top (u40); the top-trim stops before it.
    const r = planTrim({ items: mk(0, 1000), op: 'append', cap: 600, protectedUuids: new Set(['u40']), fetchInFlight: false });
    expect(r.droppedTop).toBe(40);                     // stopped at u40 (keeps u40..)
    expect(r.keep[0].anchor.uuid).toBe('u40');
    expect(r.keep).toHaveLength(960);
    expect(r.resetTopCursorTo).toBe(40);
  });

  it('protects a uuid that is a folded MEMBER, not just the anchor', () => {
    const items = mk(0, 1000);
    // Fold a protected member uuid into the otherwise-droppable bottom item u980.
    items[980] = { ...items[980], member_uuids: ['u980', 'needle'] } as ConversationItem;
    const r = planTrim({ items, op: 'prepend', cap: 600, protectedUuids: new Set(['needle']), fetchInFlight: false });
    expect(r.keep.some((it) => it.member_uuids.includes('needle'))).toBe(true);
    expect(r.keep[r.keep.length - 1].anchor.uuid).toBe('u980');
  });

  it('does NOT trim while a fetch is in flight', () => {
    const r = planTrim({ items: mk(0, 1000), op: 'prepend', cap: 600, protectedUuids: NONE, fetchInFlight: true });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(1000);
    expect(r.resetTopCursorTo).toBeNull();
    expect(r.resetBottomCursorTo).toBeNull();
  });

  it('is a no-op under the cap', () => {
    const r = planTrim({ items: mk(0, 300), op: 'append', cap: 600, protectedUuids: NONE, fetchInFlight: false });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(300);
  });

  it('is a no-op exactly AT the cap', () => {
    const r = planTrim({ items: mk(0, 600), op: 'append', cap: 600, protectedUuids: NONE, fetchInFlight: false });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(600);
  });

  it('never trims on a reset op (the window is fresh; the cap re-applies on the next page op)', () => {
    const r = planTrim({ items: mk(0, 1000), op: 'reset', cap: 600, protectedUuids: NONE, fetchInFlight: false });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(1000);
  });

  it('when EVERY page is protected, drops nothing (correctness wins over the cap)', () => {
    // All 1000 items protected → the trim can drop none even though over cap.
    const prot = new Set(Array.from({ length: 1000 }, (_, i) => `u${i}`));
    const r = planTrim({ items: mk(0, 1000), op: 'prepend', cap: 600, protectedUuids: prot, fetchInFlight: false });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(1000);
    expect(r.resetBottomCursorTo).toBeNull();
  });
});
