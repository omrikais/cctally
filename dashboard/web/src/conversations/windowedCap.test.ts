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
    const r = planTrim({ items, op: 'prepend', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: NONE, fetchInFlight: false });
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
    const r = planTrim({ items: mk(0, 1000), op: 'append', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: NONE, fetchInFlight: false });
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
    const r = planTrim({ items: mk(0, 1000), op: 'prepend', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: new Set(['u950']), fetchInFlight: false });
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
    const r = planTrim({ items: mk(0, 1000), op: 'append', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: new Set(['u40']), fetchInFlight: false });
    expect(r.droppedTop).toBe(40);                     // stopped at u40 (keeps u40..)
    expect(r.keep[0].anchor.uuid).toBe('u40');
    expect(r.keep).toHaveLength(960);
    expect(r.resetTopCursorTo).toBe(40);
  });

  it('protects a uuid that is a folded MEMBER, not just the anchor', () => {
    const items = mk(0, 1000);
    // Fold a protected member uuid into the otherwise-droppable bottom item u980.
    items[980] = { ...items[980], member_uuids: ['u980', 'needle'] } as ConversationItem;
    const r = planTrim({ items, op: 'prepend', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: new Set(['needle']), fetchInFlight: false });
    expect(r.keep.some((it) => it.member_uuids.includes('needle'))).toBe(true);
    expect(r.keep[r.keep.length - 1].anchor.uuid).toBe('u980');
  });

  it('does NOT trim while a fetch is in flight', () => {
    const r = planTrim({ items: mk(0, 1000), op: 'prepend', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: NONE, fetchInFlight: true });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(1000);
    expect(r.resetTopCursorTo).toBeNull();
    expect(r.resetBottomCursorTo).toBeNull();
  });

  it('is a no-op under the cap', () => {
    const r = planTrim({ items: mk(0, 300), op: 'append', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: NONE, fetchInFlight: false });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(300);
  });

  it('is a no-op exactly AT the cap', () => {
    const r = planTrim({ items: mk(0, 600), op: 'append', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: NONE, fetchInFlight: false });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(600);
  });

  it('never trims on a reset op (the window is fresh; the cap re-applies on the next page op)', () => {
    const r = planTrim({ items: mk(0, 1000), op: 'reset', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: NONE, fetchInFlight: false });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(1000);
  });

  it('when EVERY page is protected, drops nothing (correctness wins over the cap)', () => {
    // All 1000 items protected → the trim can drop none even though over cap.
    const prot = new Set(Array.from({ length: 1000 }, (_, i) => `u${i}`));
    const r = planTrim({ items: mk(0, 1000), op: 'prepend', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: prot, fetchInFlight: false });
    expect(r.droppedTop + r.droppedBottom).toBe(0);
    expect(r.keep).toHaveLength(1000);
    expect(r.resetBottomCursorTo).toBeNull();
  });

  it('keeps the window ABOVE the cap when a protected edge blocks a full trim (#230 P3)', () => {
    // A protected uuid in the drop zone (u100) stops the top-trim early: only
    // u0..u99 are dropped, so the kept window (900) stays OVER the cap (600). The
    // hook's dev-only telemetry (useConversation) keys on exactly this after-trim
    // `keep.length > cap` condition — a real, reachable over-cap outcome, since a
    // protected uuid is never evicted (correctness wins over the cap).
    const r = planTrim({ items: mk(0, 1000), op: 'append', capItems: 600, capBlocks: Number.POSITIVE_INFINITY, protectedUuids: new Set(['u100']), fetchInFlight: false });
    expect(r.droppedTop).toBe(100);              // trimmed up to (not into) the protected u100
    expect(r.keep[0].anchor.uuid).toBe('u100');
    expect(r.keep.length).toBeGreaterThan(600);  // window remains ABOVE the cap
    expect(r.keep).toHaveLength(900);
  });
});

// #463 S1 — the retained window becomes SIZE-aware. Before segmentation the cap
// was item-count arithmetic with no concept of blocks, so one 827-block Codex
// turn counted as a single item and the trim had never fired on a Codex
// conversation at all. Blocks are what correlate with DOM and render work, so
// they become the primary bound with the item count kept as a secondary ceiling
// for the many-tiny-items case.
function mkSized(start: number, count: number, blocksEach: number): ConversationItem[] {
  return mk(start, count).map((item) => ({
    ...item,
    blocks: Array.from({ length: blocksEach }, () => ({ kind: 'text', text: '' })),
  })) as ConversationItem[];
}
const sumBlocks = (items: ConversationItem[]) =>
  items.reduce((acc, item) => acc + (item.blocks?.length ?? 0), 0);

describe('planTrim — the block-aware retained window (#463 S1)', () => {
  it('trims on accumulated blocks even when the item count is under the cap', () => {
    const items = mkSized(0, 200, 40); // 200 items, 8,000 blocks
    const plan = planTrim({
      items, op: 'append', capBlocks: 4000, capItems: 1000,
      protectedUuids: NONE, fetchInFlight: false,
    });
    expect(plan.droppedTop).toBeGreaterThan(0);
    expect(sumBlocks(plan.keep)).toBeLessThanOrEqual(4000);
    // The bottom is the edge being paged toward, so an append drops the top.
    expect(plan.droppedBottom).toBe(0);
    expect(plan.keep[plan.keep.length - 1].anchor.uuid).toBe('u199');
  });

  it('trims the far bottom on a prepend when the block budget is exceeded', () => {
    const items = mkSized(0, 200, 40);
    const plan = planTrim({
      items, op: 'prepend', capBlocks: 4000, capItems: 1000,
      protectedUuids: NONE, fetchInFlight: false,
    });
    expect(plan.droppedBottom).toBeGreaterThan(0);
    expect(plan.droppedTop).toBe(0);
    expect(sumBlocks(plan.keep)).toBeLessThanOrEqual(4000);
    expect(plan.keep[0].anchor.uuid).toBe('u0');
  });

  it('still honours the item ceiling for many tiny items', () => {
    const items = mkSized(0, 1200, 1); // 1,200 blocks — well under capBlocks
    const plan = planTrim({
      items, op: 'append', capBlocks: 4000, capItems: 1000,
      protectedUuids: NONE, fetchInFlight: false,
    });
    expect(plan.keep.length).toBeLessThanOrEqual(1000);
  });

  it('never evicts a protected item even when that leaves the window over cap', () => {
    const items = mkSized(0, 200, 40);
    const plan = planTrim({
      items, op: 'append', capBlocks: 4000, capItems: 1000,
      protectedUuids: new Set(['u0']), fetchInFlight: false,
    });
    expect(plan.keep.some((item) => item.anchor.uuid === 'u0')).toBe(true);
    expect(sumBlocks(plan.keep)).toBeGreaterThan(4000);
  });

  it('keeps at least one item however large that item is', () => {
    const items = mkSized(0, 3, 9000);
    const plan = planTrim({
      items, op: 'append', capBlocks: 4000, capItems: 1000,
      protectedUuids: NONE, fetchInFlight: false,
    });
    expect(plan.keep.length).toBeGreaterThanOrEqual(1);
    expect(plan.keep[plan.keep.length - 1].anchor.uuid).toBe('u2');
  });

  it('is a no-op when both budgets are satisfied', () => {
    const items = mkSized(0, 50, 10); // 50 items, 500 blocks
    const plan = planTrim({
      items, op: 'append', capBlocks: 4000, capItems: 1000,
      protectedUuids: NONE, fetchInFlight: false,
    });
    expect(plan.keep).toBe(items); // reference-equal — the cheap no-op signal
  });

  it('treats an item carrying no blocks array as costing no blocks', () => {
    // The legacy Claude path builds items without a blocks array in some
    // fixtures; the item ceiling must still bind for them.
    const plan = planTrim({
      items: mk(0, 1200), op: 'append', capBlocks: 4000, capItems: 1000,
      protectedUuids: NONE, fetchInFlight: false,
    });
    expect(plan.keep.length).toBe(1000);
  });
});
