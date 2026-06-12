import type { FocusMode } from './applyFocusMode';
import type { OutlineTurn } from '../types/conversation';

// #177 S5 §5 — outline-skeleton visibility predicate. The reader's `nodeVisible`
// (applyFocusMode.ts) decides whether a turn survives a focus mode, but it
// operates on `RenderNode`s (full detail items), which the OutlinePanel does
// NOT have — the panel only carries `OutlineTurn` skeletons. So this is the
// cheap skeleton-shaped twin of `nodeVisible`, kept in lock-step with it so a
// panel jump (entry click / glyph cluster / stats error row) resets the focus
// mode to `all` ONLY when the target turn would be hidden by the current mode
// (never a silent no-op behind a focus filter). Used by OutlinePanel before it
// dispatches an in-session OPEN_CONVERSATION jump.
//
// Mapping to nodeVisible (per-turn, since the panel jumps to individual turns):
//   - all:      every turn visible.
//   - prompts:  human turns only.
//   - errors:   any turn carrying an is_error tool result (incl. orphan
//               tool_result error turns and sidechain error turns) — the same
//               itemHasError test, expressed over OutlineTurn.tools.
//   - chat:     prose-/thinking-bearing human or assistant turns; tool-only
//               turns, orphan tool_result turns, meta turns, and sidechain
//               turns are suppressed.
// A sidechain turn matches nodeVisible's subagent/tool_result_run rule: visible
// only in `errors` AND only when it carries an error.
export function outlineTurnVisible(turn: OutlineTurn, mode: FocusMode): boolean {
  if (mode === 'all') return true;
  const hasError = (turn.tools ?? []).some((x) => x.is_error);
  // Sidechain turns ride inside subagent / tool_result_run nodes: visible only
  // in errors-mode, and only when the turn itself carries an error.
  if (turn.is_sidechain) return mode === 'errors' && hasError;
  if (mode === 'prompts') return turn.kind === 'human';
  if (mode === 'errors') return hasError;
  // chat: prose-bearing human/assistant turns survive; everything else hides.
  if (turn.kind === 'human') return true;
  if (turn.kind === 'assistant') {
    return turn.label.trim() !== '' || (turn.thinking?.length ?? 0) > 0;
  }
  return false;
}

// #177 S5 §4 — jump-to-next cursor math, shared by the reader's e/u/b/p keys and
// the OutlinePanel glyph cluster. Pure: given a SORTED ascending list of target
// turn indices (outline-skeleton space), the cursor's current turn index, and a
// direction, return the next/previous target index strictly past the cursor — or
// null when there is none (no wrap). A cursor of -1 means "before the start" so a
// forward jump finds the first target.
export function nextTarget(indices: number[], cursor: number, dir: 1 | -1): number | null {
  if (dir === 1) {
    for (const i of indices) if (i > cursor) return i;
    return null;
  }
  // dir === -1: scan from the end for the first index strictly less than cursor.
  for (let k = indices.length - 1; k >= 0; k--) {
    if (indices[k] < cursor) return indices[k];
  }
  return null;
}
