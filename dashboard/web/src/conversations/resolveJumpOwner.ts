import type { TimedNode } from './insertTimeMarkers';
import type { SubagentNode } from './groupSidechains';
import type { ConversationItem } from '../types/conversation';

// #234 §2.3-1 — resolve a jump uuid to its OWNING subagent from the render tree,
// not the flat item list. The measured R2 bug: the existing
// detail.items.find((it) => it.member_uuids.includes(jump.uuid)) path left the
// enclosing subagent card un-force-opened (no conv-sidechain--force across 109
// mounted samples), so the matched member sat in un-accounted overflow outside
// the scrollable range — unreachable by any scrollTop. This returns the DEEPEST
// owning subagent key (so the caller can force-open the whole ancestor chain),
// the top-level bucket-root uuid, and whether the uuid IS the card root.
export interface JumpOwner {
  ownerSubagentKey: string | null; // deepest subagent that contains the uuid; null = top-level (not in a subagent)
  rootUuid: string;                // the TOP-LEVEL node's bucket-root uuid
  isCardRoot: boolean;             // the uuid IS the top-level subagent card's first anchor
}

const itemHas = (it: ConversationItem, uuid: string) =>
  it.anchor.uuid === uuid || it.member_uuids.includes(uuid);

// Deepest-first: a grandchild resolves to the child's key, not the root's.
function ownerInSubagent(n: SubagentNode, uuid: string): string | null {
  for (const c of n.children) {
    const inChild = ownerInSubagent(c, uuid);
    if (inChild != null) return inChild;
  }
  return n.items.some((it) => itemHas(it, uuid)) ? n.subagentKey : null;
}

/** Resolve a jump uuid to its owning subagent from the render tree (spec §2.3-1). */
export function resolveJumpOwner(nodes: TimedNode[], uuid: string): JumpOwner | null {
  for (const node of nodes) {
    if (node.kind === 'subagent') {
      const ownerSubagentKey = ownerInSubagent(node, uuid);
      if (ownerSubagentKey != null) {
        const rootUuid = node.items[0].anchor.uuid;
        return { ownerSubagentKey, rootUuid, isCardRoot: rootUuid === uuid };
      }
    } else if (node.kind === 'item') {
      if (itemHas(node.item, uuid)) return { ownerSubagentKey: null, rootUuid: node.item.anchor.uuid, isCardRoot: false };
    } else if (node.kind === 'tool_result_run') {
      if (node.items.some((it) => itemHas(it, uuid)))
        return { ownerSubagentKey: null, rootUuid: node.items[0].anchor.uuid, isCardRoot: false };
    }
  }
  return null;
}
