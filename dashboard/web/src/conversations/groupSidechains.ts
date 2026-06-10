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
  | { kind: 'subagent'; subagentKey: string; items: ConversationItem[]; nested: boolean }
  // #164: a run of >=2 consecutive top-level orphan tool_result items, collapsed
  // from the bare-result texture into one disclosure. Each member still renders
  // as its own MessageItem (per-member ref intact for #160).
  | { kind: 'tool_result_run'; items: ConversationItem[] };

// Post-pass over the render-tree: fold runs of >=2 consecutive top-level orphan
// `tool_result` item nodes into one `tool_result_run` node; a lone orphan stays
// an `item` node. Only top-level `item` nodes of kind tool_result are eligible —
// a `subagent` node (or any non-tool_result item) breaks a run. Runs that fold
// preserve member order; the surrounding nodes pass through unchanged.
function collapseToolResultRuns(nodes: RenderNode[]): RenderNode[] {
  const res: RenderNode[] = [];
  let run: ConversationItem[] = [];
  const flush = () => {
    if (run.length >= 2) res.push({ kind: 'tool_result_run', items: run });
    else if (run.length === 1) res.push({ kind: 'item', item: run[0] });
    run = [];
  };
  for (const n of nodes) {
    if (n.kind === 'item' && n.item.kind === 'tool_result') {
      run.push(n.item);
      continue;
    }
    flush();
    res.push(n);
  }
  flush();
  return res;
}

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
    // Nest on the first NON-meta item's parent (the logical task root), not
    // bucket[0] — a subagent file can open with an injected `meta` row (a skill
    // body / SessionStart injection) whose parent_uuid is an intra-file link,
    // which must not drive cross-file nesting (Codex P1.3). Fallback to b[0].
    const root = b.find((it) => it.kind !== 'meta') ?? b[0];
    const parentUuid = root.parent_uuid;
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
  // 5. Collapse residual orphan tool_result runs (#164) before returning.
  return collapseToolResultRuns(out);
}
