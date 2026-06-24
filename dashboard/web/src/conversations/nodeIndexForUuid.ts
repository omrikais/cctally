import type { TimedNode } from './insertTimeMarkers';
import type { ConversationItem } from '../types/conversation';
import type { SubagentNode } from './groupSidechains';

// Resolve a turn uuid (possibly a folded member fragment, or a turn nested inside
// a subagent subtree) to the TOP-LEVEL node that renders it, in both coordinate
// spaces (#232, Codex P0-2). The virtualIndex feeds Virtuoso APIs (scrollToIndex);
// the arrayIndex feeds local render math. Returns null when the uuid is not in the
// loaded `nodes`.
export interface NodeIndex {
  arrayIndex: number;
  virtualIndex: number;
}

function itemHasUuid(it: ConversationItem, uuid: string): boolean {
  return it.anchor.uuid === uuid || it.member_uuids.includes(uuid);
}

function subagentHasUuid(n: SubagentNode, uuid: string): boolean {
  return n.items.some((it) => itemHasUuid(it, uuid)) ||
    n.children.some((c) => subagentHasUuid(c, uuid));
}

function nodeHasUuid(n: TimedNode, uuid: string): boolean {
  switch (n.kind) {
    case 'time_marker': return false;
    case 'hidden_run': return n.firstUuid === uuid;
    case 'item': return itemHasUuid(n.item, uuid);
    case 'subagent': return subagentHasUuid(n, uuid);
    case 'tool_result_run': return n.items.some((it) => itemHasUuid(it, uuid));
    default: return false;
  }
}

export function nodeIndexForUuid(
  nodes: TimedNode[], uuid: string, firstItemIndex: number,
): NodeIndex | null {
  for (let i = 0; i < nodes.length; i++) {
    if (nodeHasUuid(nodes[i], uuid)) {
      return { arrayIndex: i, virtualIndex: firstItemIndex + i };
    }
  }
  return null;
}
