import type { ConversationItem } from '../types/conversation';

// Render-tree builder for the reader (spec §4 / #155). Main items
// (subagent_key === null) pass through as `item` nodes. Sidechain items are
// bucketed by subagent_key (one agent-*.jsonl file per subagent thread) into
// `subagent` nodes, placed in document order at each bucket's first member —
// UNLESS the bucket's root parent_uuid resolves to a loaded main item, in which
// case the group is nested right after that parent. Pure + deterministic (Map
// insertion order); recomputed by ConversationReader's useMemo as pages load.
export type RenderNode =
  | { kind: 'item'; item: ConversationItem }
  | { kind: 'subagent'; subagentKey: string; items: ConversationItem[]; nested: boolean };

export function groupSidechains(items: ConversationItem[]): RenderNode[] {
  // 1. Single pass: bucket sidechain items by subagent_key (document order,
  //    bucket[0] = root) AND index every main item's member uuid -> the main
  //    item, for nest resolution.
  const buckets = new Map<string, ConversationItem[]>();
  const mainByUuid = new Map<string, ConversationItem>();
  for (const it of items) {
    const k = it.subagent_key;
    if (k != null) {
      const b = buckets.get(k);
      if (b) b.push(it);
      else buckets.set(k, [it]);
    } else {
      for (const u of it.member_uuids) mainByUuid.set(u, it);
    }
  }

  // 2. Classify each bucket: nested iff the root's parent_uuid resolves to a
  //    loaded main item. nestedByParent maps that main item's anchor uuid ->
  //    the subagent keys to emit right after it.
  const nested = new Set<string>();
  const nestedByParent = new Map<string, string[]>();
  for (const [k, b] of buckets) {
    const parentUuid = b[0].parent_uuid;
    const parent = parentUuid != null ? mainByUuid.get(parentUuid) : undefined;
    if (parent) {
      nested.add(k);
      const pk = parent.anchor.uuid;
      const arr = nestedByParent.get(pk);
      if (arr) arr.push(k);
      else nestedByParent.set(pk, [k]);
    }
  }

  // 3. Emit in render order.
  const out: RenderNode[] = [];
  const emitted = new Set<string>();
  const emit = (k: string) => {
    out.push({ kind: 'subagent', subagentKey: k, items: buckets.get(k)!, nested: nested.has(k) });
    emitted.add(k);
  };
  for (const it of items) {
    if (it.subagent_key == null) {
      out.push({ kind: 'item', item: it });
      const kids = nestedByParent.get(it.anchor.uuid);
      if (kids) for (const k of kids) if (!emitted.has(k)) emit(k);
    } else {
      const k = it.subagent_key;
      if (emitted.has(k) || nested.has(k)) continue; // nested groups emit at parent
      emit(k); // non-nested: at the root's document position
    }
  }
  // 4. Final sweep: any bucket not yet emitted (defensive — guarantees no
  //    sidechain item is ever dropped).
  for (const k of buckets.keys()) if (!emitted.has(k)) emit(k);
  return out;
}
