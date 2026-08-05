import type { FocusMode } from './applyFocusMode';
import type { OutlineLandmark, OutlineTurn } from '../types/conversation';
import { landmarkAnchorKey, mergeLandmarks } from './mergeLandmarks';

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
// #217 S5 E4 — the three "▾ More" modes mirror nodeVisible over the
// OutlineTurn skeleton (Codex P1-5 twin): `edits`/`bash` match turn.tools by
// (lower-cased) name; `subagent:<key>` matches turn.subagent_key. Kept lower-
// cased here to match applyFocusMode's EDIT_TOOLS/BASH_TOOLS sets.
const TWIN_EDIT_TOOLS = new Set(['edit', 'multiedit', 'write', 'apply_patch', 'patch_apply_end']);
const TWIN_BASH_TOOLS = new Set(['bash', 'exec']);

export function outlineTurnVisible(turn: OutlineTurn, mode: FocusMode): boolean {
  if (mode === 'all') return true;
  // subagent:<key> — a turn is visible iff it carries the matching key (covers
  // both sidechain subagent turns and a main-thread turn stamped with the key).
  if (mode.startsWith('subagent:')) {
    return turn.subagent_key === mode.slice('subagent:'.length);
  }
  const turnHasTool = (names: Set<string>) =>
    (turn.tools ?? []).some((x) => !!x.name && names.has(x.name.toLowerCase()));
  if (mode === 'edits') return turnHasTool(TWIN_EDIT_TOOLS);
  if (mode === 'bash') return turnHasTool(TWIN_BASH_TOOLS);
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
// #217 S3 F8 — 'compaction' added: the compaction-landmark jump family.
// #217 S6 F4 — 'bookmark' added: the client-only bookmark jump family. Its index
// list is built from the explicitly-passed `bookmarks` param (OutlineTurn has no
// bookmark field), NOT from the server skeleton.
export type JumpKind = 'error' | 'prompt' | 'subagent' | 'plan' | 'cache' | 'compaction' | 'bookmark';

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
// #463 S4 §6.3 — one jump target. It used to be a bare numeric index into the
// array `buildOutlineTargets` was given, and THREE sites dereferenced that
// against tier-1 `turns`: `JumpCluster`'s `jumpToIndex` and two sites in
// `ConversationReader`. A tier-2 landmark's jump has to load a SEGMENT while
// those three still resolve the owning turn, so the target carries both.
//
// `ownerTurnIndex` indexes the tier-1 `turns` array this was built from, never
// a merged list — which is what lets the two arrays have different lengths
// without any consumer having to know.
export interface OutlineTarget {
  anchorKey: string;
  ownerTurnIndex: number;
  // #463 S4 F-A — the finer address INSIDE the loaded item. The browser gate
  // measured a jump putting the right segment at the top of a 635px viewport
  // with the failure it named up to 6,574px below the fold, because one Codex
  // segment can be 4,098px tall and hold several failing calls. ABSENT on every
  // tier-1 target, so the pre-S4 landing is unchanged.
  innerAnchorKey?: string;
}

export interface OutlineTargets {
  error: OutlineTarget[];
  prompt: OutlineTarget[];
  subagent: OutlineTarget[];
  plan: OutlineTarget[];
  cache: OutlineTarget[];
  // #217 S3 F8 — turns the parser stamped meta_kind 'compaction' (#191).
  compaction: OutlineTarget[];
  // #217 S6 F4 — the bookmarked turns in document order. Built from the client
  // `bookmarks` param (not the skeleton), so it is [] when none are passed.
  bookmark: OutlineTarget[];
  // Every turn's OWN uuid → its skeleton index (cursor resolution).
  indexByUuid: Map<string, number>;
  // #217 S3 E2 (Codex P1) — every MEMBER (folded-fragment) uuid → its owning
  // turn's skeleton index, so `loadToTarget` can normalize a deep-link / search
  // uuid that is a folded fragment to its owning turn before deciding direction.
  // Populated from each turn's `member_uuids`; on a member-uuid collision across
  // turns the FIRST occurrence wins (document order). The own-uuid map is
  // authoritative — `resolveTurnIndex` checks it first.
  memberIndex: Map<string, number>;
  // #463 S1 — every SEGMENT key → its owning turn's skeleton index. A turn that
  // segmentation split is still one outline entry, and only its segment 0 key
  // appears as that entry's own uuid, so a deep link, find hit or reading
  // position naming segment 1..N would resolve to no turn at all and
  // `loadToTarget` would no-op. This map is deliberately SEPARATE from
  // `memberIndex`: a segment key must never enter `member_uuids`, because
  // `loadToTarget` reads that as "already loaded".
  segmentIndex: Map<string, number>;
}

export function buildOutlineTargets(
  turns: OutlineTurn[],
  // #217 S6 F4 — the current session's bookmarks (uuid → anything truthy). Only
  // the KEYS matter here; a turn whose own uuid is a bookmark key pushes its
  // index onto the `bookmark` list. Absent → no bookmark targets.
  bookmarks?: Record<string, unknown>,
  // #463 S4 §3.2 — the tier-2 landmarks. When present, the error and plan
  // families come from them, so a jump lands on the failing call's segment
  // rather than on the top of a turn that may hold 142 calls. `deriveOutline`
  // gates on the same presence, so the rendered rail and the jump stops can
  // never disagree about which granularity is in force.
  landmarks?: readonly OutlineLandmark[],
): OutlineTargets {
  const error: OutlineTarget[] = [];
  const prompt: OutlineTarget[] = [];
  const subagent: OutlineTarget[] = [];
  const plan: OutlineTarget[] = [];
  const cache: OutlineTarget[] = [];
  const compaction: OutlineTarget[] = [];
  const bookmark: OutlineTarget[] = [];
  const indexByUuid = new Map<string, number>();
  const memberIndex = new Map<string, number>();
  const segmentIndex = new Map<string, number>();
  const seenSub = new Set<string>();
  // Gated on landmark PRESENCE rather than on a source string, matching
  // `deriveOutline`: a Claude outline receives none and keeps today's behaviour.
  const landmarkAware = !!landmarks?.length;
  turns.forEach((t, i) => {
    indexByUuid.set(t.uuid, i);
    // Map each member fragment uuid (falling back to the turn's own uuid for a
    // turn with an empty/missing member list) to this turn — first-occurrence
    // wins so a duplicate member uuid never re-points a later turn.
    for (const u of (t.member_uuids?.length ? t.member_uuids : [t.uuid])) {
      if (!memberIndex.has(u)) memberIndex.set(u, i);
    }
    for (const u of (t.segment_uuids ?? [])) {
      if (!segmentIndex.has(u)) segmentIndex.set(u, i);
    }
    const own = { anchorKey: t.uuid, ownerTurnIndex: i };
    if (!landmarkAware && t.tools?.some((x) => x.is_error)) error.push(own);
    if (t.kind === 'human') prompt.push(own);
    if (t.subagent_key != null && !seenSub.has(t.subagent_key)) {
      seenSub.add(t.subagent_key);
      subagent.push(own); // FIRST turn index per distinct subagent_key
    }
    if (!landmarkAware
        && t.tools?.some((x) => x.name != null && PLAN_QUESTION_TOOLS.has(x.name))) {
      plan.push(own);
    }
    if (t.cache_failure) cache.push(own);
    // #217 S3 F8 — compaction-summary turns (parser stamp, #191).
    if (t.kind === 'meta' && t.meta_kind === 'compaction') compaction.push(own);
    // #217 S6 F4 — a turn whose own uuid is a bookmark key (client-only).
    if (bookmarks && t.uuid in bookmarks) bookmark.push(own);
  });
  // Landmark families, in owning-turn order so the stepping math below still
  // sees an ascending list. The owner comes from the landmark's own
  // `parent_uuid` rather than from `segmentIndex`, because `parent_uuid` is
  // authoritative: the server states it, while `segmentIndex` depends on the
  // owning turn having survived the adapter's navigation filter.
  for (const { landmark, ownerTurnIndex } of mergeLandmarks(turns, landmarks)) {
    const target = {
      anchorKey: landmark.uuid, ownerTurnIndex,
      innerAnchorKey: landmarkAnchorKey(landmark),
    };
    if (landmark.kind === 'tool_error') error.push(target);
    else if (landmark.kind === 'plan') plan.push(target);
  }
  return {
    error, prompt, subagent, plan, cache, compaction, bookmark,
    indexByUuid, memberIndex, segmentIndex,
  };
}

// #184's cursor math, over the S4 target shape. It delegates to `nextTarget` so
// the two cannot drift, then names the target that index belongs to.
//
// RESIDUAL, stated rather than hidden: stepping is TURN-granular, because the
// cursor is a turn index and several landmarks of one turn share theirs. A
// forward step lands on the first landmark of the next owning turn and a
// backward step on the last landmark of the previous one, so a turn holding
// three failures is one stop rather than three. Every one of the three is still
// a rail row and still reachable by clicking it, and the chip's primary click
// lands on the last target directly.
export function nextTargetEntry(
  targets: readonly OutlineTarget[], cursor: number, dir: 1 | -1,
): OutlineTarget | null {
  const index = nextTarget(targets.map((target) => target.ownerTurnIndex), cursor, dir);
  if (index == null) return null;
  const matches = targets.filter((target) => target.ownerTurnIndex === index);
  return (dir === 1 ? matches[0] : matches[matches.length - 1]) ?? null;
}

// #463 S4 F-B — how many TURNS a family's targets sit in. Under landmark
// awareness a family holds one target per failing call, while the chip's
// wording ("error turns"), the stats card's reconciliation phrase ("14 errors
// in 13 turns") and the stepping this module offers are all turn-granular. The
// browser gate saw a chip reading `error turns 17` on a conversation whose
// headline read "2 turns", because the two numbers had become equal by
// construction. For a Claude family — one target per turn — this returns the
// array length, so nothing on that side moves.
export function distinctOwnerTurns(targets: readonly OutlineTarget[]): number {
  return new Set(targets.map((target) => target.ownerTurnIndex)).size;
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
  const member = targets.memberIndex.get(uuid);
  if (member !== undefined) return member;
  // #463 S1 — last, a segment key. The outline stays turn-granular, so a
  // segment past the first resolves to its turn's skeleton index, which is all
  // `loadToTarget` needs to choose a paging direction; the drain then stops
  // once the segment's own key appears in a loaded item's `member_uuids`.
  return targets.segmentIndex.get(uuid);
}

// #463 S4 remediation — the cursor a step counts from, for EVERY entry point.
//
// The browser gate found forward stepping re-landing on the first target
// forever and backward stepping finding nothing at all. Both entry points that
// step — the outline's jump chip and the reader's e/E,u/U,b/B,p/P keys — read
// `indexByUuid` directly, and that map holds a turn's OWN uuid only. A landmark
// jump pins the SEGMENT it landed on, so the lookup missed, the cursor stayed
// at -1, and `nextTarget` answered "the first target" forward and "none"
// backward on every press. The prompt family kept stepping correctly through
// all of it, because its anchors are turn uuids, which is why every unit gate
// stayed green.
//
// `resolveTurnIndex` already consults own uuids, then member uuids, then
// segment keys. This wraps it in the "no cursor at all" convention `nextTarget`
// documents, so no caller has to re-derive either half.
export function cursorIndex(
  targets: OutlineTargets, uuid: string | null | undefined,
): number {
  if (uuid == null) return -1;
  return resolveTurnIndex(targets, uuid) ?? -1;
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
