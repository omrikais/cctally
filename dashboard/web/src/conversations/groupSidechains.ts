import type { ConversationItem, SubagentMeta } from '../types/conversation';

// Render-tree builder for the reader (spec §4 / §5 / #155). Main items
// (subagent_key === null) pass through as `item` nodes. Sidechain items are
// bucketed by subagent_key (one agent-*.jsonl file per subagent thread) into
// recursive `subagent` nodes. Parent resolution prefers the kernel's read-time
// linkage (subagent_meta[k].parent_subagent_key + spawn_uuid): a bucket whose
// parent is another LOADED subagent nests inside that parent's node (a
// grandchild under a child); a bucket whose parent is null/main nests at its
// spawn_uuid anchor in the main stream; an unresolved parent stays top-level.
// Old transcripts carry no kernel linkage → fall back to the legacy
// root.parent_uuid -> main item resolution. Pure + deterministic (Map insertion
// order); recomputed by ConversationReader's useMemo as pages load.

// §5 — a recursive subagent render node. `children` are the subagent threads
// spawned from INSIDE this one (resolved via parent_subagent_key); they render
// nested under this node, interleaved after the member item matching each
// child's spawnAnchorUuid. `depth` keys the visual indent (0 = top-level).
export interface SubagentNode {
  kind: 'subagent';
  subagentKey: string;
  items: ConversationItem[];
  nested: boolean;             // true when placed under a parent (not top-level/orphan)
  depth: number;               // 0 = top-level subagent; +1 per nesting level
  spawnAnchorUuid: string | null;  // parent-thread item uuid to render this node AFTER (null = append)
  children: SubagentNode[];    // child subagent threads spawned from inside this one
}

export type RenderNode =
  | { kind: 'item'; item: ConversationItem }
  | SubagentNode
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

export function groupSidechains(
  items: ConversationItem[],
  subagentMeta?: Record<string, SubagentMeta>,
): RenderNode[] {
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

  // 2. Resolve each bucket's parent. Prefer kernel linkage (parent_subagent_key
  //    + spawn_uuid); fall back to the legacy root.parent_uuid -> main item.
  type Parent =
    | { where: 'main'; anchorUuid: string }
    | { where: 'subagent'; parentKey: string; anchorUuid: string | null }
    | { where: 'top' };
  const parentOf = new Map<string, Parent>();
  for (const k of buckets.keys()) {
    const m = subagentMeta?.[k];
    if (m && m.parent_subagent_key !== undefined) {
      // Kernel linkage present (new format). parent_subagent_key === null means
      // a main-session parent; a hash means a parent subagent thread.
      const pk = m.parent_subagent_key ?? null;
      const anchor = m.spawn_uuid ?? null;
      if (pk != null && buckets.has(pk)) {
        parentOf.set(k, { where: 'subagent', parentKey: pk, anchorUuid: anchor });
      } else if (pk == null && anchor != null && mainByUuid.has(anchor)) {
        parentOf.set(k, { where: 'main', anchorUuid: anchor });
      } else {
        // parent not loaded (paged out) or main anchor missing -> top-level.
        parentOf.set(k, { where: 'top' });
      }
      continue;
    }
    // Legacy fallback (old transcripts: no kernel linkage). Nest on the first
    // NON-meta item's parent (the logical task root), not bucket[0] — a subagent
    // file can open with an injected `meta` row (a skill body / SessionStart
    // injection) whose parent_uuid is an intra-file link, which must not drive
    // cross-file nesting (Codex P1.3). Fallback to b[0].
    const b = buckets.get(k)!;
    const root = b.find((it) => it.kind !== 'meta') ?? b[0];
    const parent = root.parent_uuid != null ? mainByUuid.get(root.parent_uuid) : undefined;
    parentOf.set(k, parent ? { where: 'main', anchorUuid: parent.anchor.uuid } : { where: 'top' });
  }

  // 3. Build subagent nodes; recursively attach child subagents. childKeysOf
  //    maps a parent subagent key -> the keys of buckets nested inside it.
  const nodeOf = new Map<string, SubagentNode>();
  const childKeysOf = new Map<string, string[]>();
  for (const [k, p] of parentOf) {
    if (p.where === 'subagent') {
      const arr = childKeysOf.get(p.parentKey);
      if (arr) arr.push(k);
      else childKeysOf.set(p.parentKey, [k]);
    }
  }
  const build = (k: string, depth: number): SubagentNode => {
    const existing = nodeOf.get(k);
    if (existing) return existing;
    const p = parentOf.get(k)!;
    const node: SubagentNode = {
      kind: 'subagent',
      subagentKey: k,
      items: buckets.get(k)!,
      nested: p.where !== 'top',
      depth,
      spawnAnchorUuid: p.where === 'top' ? null : p.anchorUuid ?? null,
      children: [],
    };
    nodeOf.set(k, node);
    node.children = (childKeysOf.get(k) ?? []).map((ck) => build(ck, depth + 1));
    return node;
  };

  // 4. Emit the TOP LEVEL in document order. Main items pass through; a 'main'-
  //    parented subagent interleaves right after its anchor main item; a 'top'
  //    subagent emits at its root's document position. A 'subagent'-parented
  //    bucket is NOT emitted at top level — it lives inside its parent's
  //    children (placed recursively by SidechainGroup). markEmitted recurses so
  //    a parented bucket already attached to a parent node is not re-emitted by
  //    the document walk or the final no-drop sweep.
  const nestedByMain = new Map<string, string[]>();   // main anchor uuid -> top-level subagent keys
  for (const [k, p] of parentOf) {
    if (p.where === 'main') {
      const a = nestedByMain.get(p.anchorUuid);
      if (a) a.push(k);
      else nestedByMain.set(p.anchorUuid, [k]);
    }
  }
  const out: RenderNode[] = [];
  const emitted = new Set<string>();
  const markEmitted = (k: string) => {
    emitted.add(k);
    for (const ck of childKeysOf.get(k) ?? []) markEmitted(ck);
  };
  const emitTop = (k: string) => { out.push(build(k, 0)); markEmitted(k); };
  for (const it of items) {
    if (it.subagent_key == null) {
      out.push({ kind: 'item', item: it });
      for (const k of nestedByMain.get(it.anchor.uuid) ?? []) if (!emitted.has(k)) emitTop(k);
    } else {
      const k = it.subagent_key;
      if (emitted.has(k)) continue;                       // already placed (nested or earlier)
      const p = parentOf.get(k)!;
      if (p.where === 'top') emitTop(k);                  // orphan: document position
      // else a 'main'/'subagent'-parented bucket — placed via its parent (the
      // main anchor above, or recursively inside its parent subagent node).
    }
  }
  // 5. Final sweep: any bucket not yet emitted (defensive — guarantees no
  //    sidechain item is ever dropped, e.g. a 'main' anchor that never loaded).
  for (const k of buckets.keys()) if (!emitted.has(k)) emitTop(k);
  // 6. Collapse residual orphan tool_result runs (#164) before returning. Only
  //    the TOP-LEVEL list is collapsed (a subagent node breaks a run).
  return collapseToolResultRuns(out);
}

// §5 — depth-first walk over every subagent node in a render tree, INCLUDING
// nested children. Used by the reader's jump / visibility / seen traversals
// (which formerly scanned only the top-level node list) and by tests so a
// nested subagent's anchor still resolves. Visits parents before their
// children (document/tree order).
export function walkSubagents(
  nodes: RenderNode[] | SubagentNode[],
  fn: (node: SubagentNode) => void,
): void {
  for (const n of nodes) {
    if (n.kind === 'subagent') {
      fn(n);
      if (n.children.length) walkSubagents(n.children, fn);
    }
  }
}

// §5 — flatten a render tree to every subagent node (top-level + nested), in
// depth-first order. Convenience over walkSubagents for callers that want an
// array to `.find()` over (the reader's jump-target resolution).
export function flattenSubagents(nodes: RenderNode[]): SubagentNode[] {
  const out: SubagentNode[] = [];
  walkSubagents(nodes, (n) => out.push(n));
  return out;
}
