import type { RenderNode } from './groupSidechains';
import type { ConversationItem, ConversationBlock } from '../types/conversation';

// #177 S5 §5 — focus-mode filter over the reader's render-tree (RenderNode[]).
// Pure + deterministic. `all` short-circuits to the SAME array identity so the
// reader's render path stays byte-identical to today (Codex: no churn on the
// default mode). Every other mode walks the nodes, keeping the visible ones and
// coalescing each maximal run of suppressed nodes into ONE `hidden_run` marker
// (a "· N hidden ·" button the reader renders; clicking it resets to `all` and
// jumps to the first hidden node). `count` counts hidden NODES, so a subagent or
// tool_result_run node counts as exactly 1.
export type FocusMode = 'all' | 'chat' | 'prompts' | 'errors';

export type FilteredNode =
  | RenderNode
  | { kind: 'hidden_run'; count: number; firstUuid: string };

function itemHasError(it: ConversationItem): boolean {
  return it.blocks.some((b: ConversationBlock) =>
    (b.kind === 'tool_call' && !!b.result?.is_error) ||
    (b.kind === 'tool_result' && b.is_error));
}

function itemHasProseOrThinking(it: ConversationItem): boolean {
  return it.text.trim() !== '' || it.blocks.some((b) => b.kind === 'thinking');
}

// Whether a single render node survives a given (non-`all`) focus mode. Exported
// so the jump-to-next logic can reuse the EXACT predicate when deciding whether
// the current mode would hide a jump target (spec §5: reset to `all` only when
// the target is hidden).
export function nodeVisible(n: RenderNode, mode: FocusMode): boolean {
  if (mode === 'all') return true;
  if (n.kind === 'subagent') return mode === 'errors' && n.items.some(itemHasError);
  if (n.kind === 'tool_result_run') return mode === 'errors' && n.items.some(itemHasError);
  const it = n.item;
  if (mode === 'prompts') return it.kind === 'human';
  if (mode === 'errors') return itemHasError(it);
  // chat: prose-bearing human/assistant turns; tools/orphans/meta suppressed.
  if (it.kind === 'human') return true;
  if (it.kind === 'assistant') return itemHasProseOrThinking(it);
  return false;
}

// The jump anchor uuid for any filtered node shape.
export function nodeUuid(n: FilteredNode): string {
  if (n.kind === 'hidden_run') return n.firstUuid;
  if (n.kind === 'item') return n.item.anchor.uuid;
  return n.items[0].anchor.uuid;
}

export function applyFocusMode(nodes: RenderNode[], mode: FocusMode): FilteredNode[] {
  if (mode === 'all') return nodes;
  const out: FilteredNode[] = [];
  let hidden: RenderNode[] = [];
  const flush = () => {
    if (hidden.length) {
      out.push({ kind: 'hidden_run', count: hidden.length, firstUuid: nodeUuid(hidden[0]) });
    }
    hidden = [];
  };
  for (const n of nodes) {
    if (nodeVisible(n, mode)) {
      flush();
      out.push(n);
    } else {
      hidden.push(n);
    }
  }
  flush();
  return out;
}
