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

// #184 — jump-target kinds the cluster + reader keys navigate. Sorted-ascending
// index lists in outline-skeleton space, one per landmark family.
// cache-failure-markers spec §4 — 'cache' added: the flagged-turn jump family.
export type JumpKind = 'error' | 'prompt' | 'subagent' | 'plan' | 'cache';

// The tools whose presence marks a turn as a plan / question landmark.
export const PLAN_QUESTION_TOOLS = new Set(['ExitPlanMode', 'AskUserQuestion']);

// #184 — single source of truth for the jump-target machinery. The reader
// (e/u/b/p keys) and the OutlinePanel glyph cluster both navigate the SAME four
// landmark lists + the uuid→index map; this builder is the shared origin so the
// two surfaces can never drift. Pure over the outline-skeleton turns:
//   - error:    turns carrying any is_error tool result.
//   - prompt:   human turns.
//   - subagent: the FIRST turn index per distinct (non-null) subagent_key.
//   - plan:     turns carrying an ExitPlanMode / AskUserQuestion tool.
//   - cache:    turns carrying a cache_failure flag (spec §4). RAW list — the
//               markersEnabled opt-out is applied by the consumers (the cluster
//               filters the chip; the reader's `c` key no-ops) so the navigation
//               and the gating stay self-consistent with deriveOutline.
//   - indexByUuid: every turn's uuid → its skeleton index, for cursor resolution.
export interface OutlineTargets {
  error: number[];
  prompt: number[];
  subagent: number[];
  plan: number[];
  cache: number[];
  // Every turn's OWN uuid → its skeleton index (cursor resolution).
  indexByUuid: Map<string, number>;
  // #217 S3 E2 (Codex P1) — every MEMBER (folded-fragment) uuid → its owning
  // turn's skeleton index, so `loadToTarget` can normalize a deep-link / search
  // uuid that is a folded fragment to its owning turn before deciding direction.
  // Populated from each turn's `member_uuids`; on a member-uuid collision across
  // turns the FIRST occurrence wins (document order). The own-uuid map is
  // authoritative — `resolveTurnIndex` checks it first.
  memberIndex: Map<string, number>;
}

export function buildOutlineTargets(turns: OutlineTurn[]): OutlineTargets {
  const error: number[] = [];
  const prompt: number[] = [];
  const subagent: number[] = [];
  const plan: number[] = [];
  const cache: number[] = [];
  const indexByUuid = new Map<string, number>();
  const memberIndex = new Map<string, number>();
  const seenSub = new Set<string>();
  turns.forEach((t, i) => {
    indexByUuid.set(t.uuid, i);
    // Map each member fragment uuid (falling back to the turn's own uuid for a
    // turn with an empty/missing member list) to this turn — first-occurrence
    // wins so a duplicate member uuid never re-points a later turn.
    for (const u of (t.member_uuids?.length ? t.member_uuids : [t.uuid])) {
      if (!memberIndex.has(u)) memberIndex.set(u, i);
    }
    if (t.tools?.some((x) => x.is_error)) error.push(i);
    if (t.kind === 'human') prompt.push(i);
    if (t.subagent_key != null && !seenSub.has(t.subagent_key)) {
      seenSub.add(t.subagent_key);
      subagent.push(i); // FIRST turn index per distinct subagent_key
    }
    if (t.tools?.some((x) => x.name != null && PLAN_QUESTION_TOOLS.has(x.name))) plan.push(i);
    if (t.cache_failure) cache.push(i);
  });
  return { error, prompt, subagent, plan, cache, indexByUuid, memberIndex };
}

// #217 S3 E2 (Codex P1) — resolve a (possibly folded-fragment) uuid to its
// owning outline-turn skeleton index. The OWN-uuid map (`indexByUuid`) is
// authoritative (a turn that lists another turn's uuid as a member must not
// shadow the real owner); the member map is the fallback for a uuid that is only
// a folded fragment. `undefined` when the uuid belongs to no outline turn (a
// graceful no-op jump). `loadToTarget` calls this before choosing a nearest-edge
// paging direction.
export function resolveTurnIndex(targets: OutlineTargets, uuid: string): number | undefined {
  const own = targets.indexByUuid.get(uuid);
  if (own !== undefined) return own;
  return targets.memberIndex.get(uuid);
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
