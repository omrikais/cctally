import type { RenderNode, SubagentNode } from './groupSidechains';
import type { ConversationItem, ConversationBlock } from '../types/conversation';

// #177 S5 §5 — focus-mode filter over the reader's render-tree (RenderNode[]).
// Pure + deterministic. `all` short-circuits to the SAME array identity so the
// reader's render path stays byte-identical to today (Codex: no churn on the
// default mode). Every other mode walks the nodes, keeping the visible ones and
// coalescing each maximal run of suppressed nodes into ONE `hidden_run` marker
// (a "· N hidden ·" button the reader renders; clicking it resets to `all` and
// jumps to the first hidden node). `count` counts hidden NODES, so a subagent or
// tool_result_run node counts as exactly 1.
// #217 S5 E4 — the focus axis gains three "▾ More" modes alongside the four
// primary ones. `edits`/`bash` are tool-type filters; `subagent:<key>` is a
// string-encoded per-subagent filter (the key comes from the loaded top-level
// subagent groups/items). Single-select — the store slice stays one string.
export type FocusMode = 'all' | 'chat' | 'prompts' | 'errors' | 'edits' | 'bash' | `subagent:${string}`;

export type FilteredNode =
  | RenderNode
  | { kind: 'hidden_run'; count: number; firstUuid: string };

// The three named edit tools — DELIBERATELY narrower than the kernel's
// `_FILE_TOUCH_TOOLS` (which also includes NotebookEdit; out of scope, Codex
// P2-4). Lower-cased for case-insensitive block-name matching.
const EDIT_TOOLS = new Set(['edit', 'multiedit', 'write', 'apply_patch', 'patch_apply_end']);
const BASH_TOOLS = new Set(['bash', 'exec']);

function itemHasError(it: ConversationItem): boolean {
  return it.blocks.some((b: ConversationBlock) =>
    (b.kind === 'tool_call' && !!b.result?.is_error) ||
    (b.kind === 'tool_result' && b.is_error));
}

function itemHasProseOrThinking(it: ConversationItem): boolean {
  return it.text.trim() !== '' || it.blocks.some((b) => b.kind === 'thinking');
}

// True when the item carries a tool_call whose (case-insensitive) name is in
// `names` (#217 S5 E4 — the edits/bash predicate).
function itemHasToolNamed(it: ConversationItem, names: Set<string>): boolean {
  return it.blocks.some((b: ConversationBlock) =>
    b.kind === 'tool_call' && !!b.name && names.has(b.name.toLowerCase()));
}

// True when this subagent OR any nested descendant has an erroring item. A
// top-level subagent whose own body is error-free but whose grandchild errored
// must stay visible in `errors` mode (the children render inside this node, so
// hiding the ancestor would drop the only place the error shows). The render
// tree is acyclic by construction (groupSidechains' build() cycle guard), so
// the recursion terminates.
function subagentHasError(n: SubagentNode): boolean {
  return n.items.some(itemHasError) || n.children.some(subagentHasError);
}

// True when this subagent OR any nested descendant has a tool_call named in
// `names` — the edits/bash analogue of subagentHasError, so a top-level
// subagent whose body is tool-free but whose grandchild ran the tool stays
// visible (the descendant renders inside the node).
function subagentHasTool(n: SubagentNode, names: Set<string>): boolean {
  return n.items.some((it) => itemHasToolNamed(it, names)) ||
    n.children.some((c) => subagentHasTool(c, names));
}

// Whether a single render node survives a given (non-`all`) focus mode. Exported
// so the jump-to-next logic can reuse the EXACT predicate when deciding whether
// the current mode would hide a jump target (spec §5: reset to `all` only when
// the target is hidden).
export function nodeVisible(n: RenderNode, mode: FocusMode): boolean {
  if (mode === 'all') return true;
  // #217 S5 E4 — subagent:<key> filters at the TOP-LEVEL node (Codex P1-3: no
  // nested-grandchild isolation). A subagent node matches by its own key; a
  // main-thread item node matches if its item carries that subagent_key.
  if (mode.startsWith('subagent:')) {
    const key = mode.slice('subagent:'.length);
    if (n.kind === 'subagent') return n.subagentKey === key;
    if (n.kind === 'tool_result_run') return false;
    return n.item.subagent_key === key;
  }
  if (mode === 'edits') {
    if (n.kind === 'subagent') return subagentHasTool(n, EDIT_TOOLS);
    if (n.kind === 'tool_result_run') return false;
    return itemHasToolNamed(n.item, EDIT_TOOLS);
  }
  if (mode === 'bash') {
    if (n.kind === 'subagent') return subagentHasTool(n, BASH_TOOLS);
    if (n.kind === 'tool_result_run') return false;
    return itemHasToolNamed(n.item, BASH_TOOLS);
  }
  if (n.kind === 'subagent') return mode === 'errors' && subagentHasError(n);
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
