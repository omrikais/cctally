import type { CacheFailure, OutlineTaskCompletion, OutlineTurn, SubagentMeta } from '../types/conversation';
import { fmt } from '../lib/fmt';

// cache-failure-markers spec §4 — the standalone cache-landmark label:
// "cache rebuilt · 130K · ~$0.75". Uppercase-K humanized tokens (fmt.compact)
// to match the spec's outline phrasing; ~$ wasted via fmt.usd2.
function cacheLabel(cf: CacheFailure): string {
  return `cache rebuilt · ${fmt.compact(cf.tokens_recreated, { upper: true })} · ~${fmt.usd2(cf.est_wasted_usd)}`;
}

// #186 §3 — outline rail = direction C ("prompt spine + curated landmarks").
// Pure curation over the server's per-turn skeleton. Curation policy lives HERE
// (client side), not in the kernel: which turns are landmarks, their labels,
// depth, glyph type.
//
// SECTION WALK: each human (non-meta) prompt opens a depth-0 SECTION that runs
// until the next prompt. Under the open section only CURATED landmarks emit at
// depth 1, in document order:
//   - error turns (any is_error tool / orphan tool_result error) → "tool error
//     · <tool>" (or "tool error" when the tool name is null);
//   - ExitPlanMode → "plan";
//   - AskUserQuestion → "question";
//   - a Markdown-heading-led assistant turn (`/^#{1,6}\s+\S/` on t.label) → the
//     heading line verbatim;
//   - a subagent bucket → "subagent · <kind>", placed at the bucket's document
//     position in whatever section is open (NOT nested under a parent_uuid — that
//     reader-side nesting could resolve to a now-dropped generic assistant and
//     orphan the row; Codex P2a).
// Type precedence: error > plan > question > heading > subagent. An errored
// heading/plan keeps its label but takes the red error flag.
//
// Generic assistant prose turns (none of the above) emit NO row, but their
// `thinking` blocks accrue to the section prompt's `thinkingCount` (rendered as
// `🧠 ×N`). `meta_kind === 'command'` turns emit nothing.
//
// `sectionByUuid` maps EVERY turn's member_uuids (and every subagent-bucket
// member's member_uuids) → the enclosing section prompt's uuid, so the panel's
// scroll-sync can highlight the section prompt even when the topmost rendered
// element is a folded fragment or a sidechain turn. Turns before the first
// prompt map to nothing; their landmarks emit at depth 0. A zero-human session
// has no sections.
//
// Bucket RESOLUTION mirrors groupSidechains EXACTLY (bucket by subagent_key over
// the WHOLE list); only placement/depth differs (document position in the open
// section, never nested under a possibly-dropped parent).
export interface OutlineEntry {
  // entryId = RENDER IDENTITY: stable + unique per entry (React keys,
  // aria-current). uuid = JUMP ANCHOR (scroll target). For prompts and landmark
  // turns the two coincide (t.uuid); subagent buckets use `sc:${k}` for the
  // entryId (already unique) and the bucket root uuid as the jump anchor.
  entryId: string;
  uuid: string;                       // jump target (anchor uuid / bucket root)
  // #217 S3 F8 — 'compaction' added (a compaction-summary turn, meta_kind
  // 'compaction'); it joins the existing landmark family. #217 S5 F7 —
  // 'completion' (a "✓ Session complete (N tasks)" landmark at the final
  // main-thread task snapshot, emitted only when all_done).
  // #217 S6 F4 — 'bookmark' added: a client-only ★ landmark for a bookmarked
  // turn (entryId `bm:<uuid>`, jump anchor = the bare turn uuid). Emitted by the
  // bookmark pass below from the `bookmarks` param, NOT from the server skeleton.
  type: 'human' | 'heading' | 'subagent' | 'error' | 'plan' | 'question' | 'cache' | 'compaction' | 'completion' | 'bookmark';
  label: string;
  // 0 = prompt / pre-prompt landmark; 1 = section landmark. #217 S3 E6(c) — a
  // NESTED subagent (one whose parent is ANOTHER subagent bucket) renders one
  // level deeper than its parent, so depth can exceed 1 in the tree case; a
  // session with no subagent→subagent nesting stays 0|1 (degenerate-to-flat).
  depth: number;
  // #217 S3 E6(c) — tree linkage: the entryId of the parent SUBAGENT bucket when
  // this entry is a nested sub-subagent; ABSENT on every flat (top-level) entry
  // so the no-nesting output is byte-identical to the pre-change flat shape.
  parentEntryId?: string;
  error: boolean;
  plan: boolean;                      // ExitPlanMode present
  question: boolean;                  // AskUserQuestion present
  // cache-failure-markers spec §4 — set on ANY flagged row: a standalone
  // 'cache' landmark, a coinciding landmark (plan/error/heading) that keeps its
  // own type but takes the flag, or a subagent bucket whose thread carried a
  // failure. Renders a trailing amber ⚡ suffix. `cacheInfo` carries the
  // tokens/$ for the row label + the jump list; absent on unflagged rows.
  cache?: boolean;
  cacheInfo?: { tokens_recreated: number; est_wasted_usd: number };
  thinkingCount: number;              // prompt rows: total thinking blocks in the section; 0 otherwise
  toolCount: number;
  subagentKey?: string;
  subagentKind?: string;
  turnIndex: number;                  // skeleton index (cursor math)
}

export interface DerivedOutline {
  entries: OutlineEntry[];
  sectionByUuid: Map<string, string>; // member uuid → enclosing section prompt uuid
}

const PLAN_TOOLS = new Set(['ExitPlanMode']);
const QUESTION_TOOLS = new Set(['AskUserQuestion']);
// A Markdown heading: 1-6 '#', then whitespace, then a non-space char. Anchored
// to the start of the turn's first line (t.label is already the first line).
const HEADING_RE = /^#{1,6}\s+\S/;

export function deriveOutline(
  turns: OutlineTurn[],
  subagentMeta: Record<string, SubagentMeta> | undefined,
  // cache-failure-markers spec §4 — opt-out gate. Default true (opt-out): when
  // off, cache curation is skipped ENTIRELY (no standalone rows, no flags, no
  // suffixes) so the outline counts + navigation stay self-consistent.
  markersEnabled = true,
  // #217 S5 F7 (Codex P1-8) — the server's main-thread task-completion. A
  // `completion` landmark is emitted at `anchor_uuid` ONLY when `all_done`;
  // null/absent (no main-thread tasks, or an older payload) → no landmark.
  taskCompletion?: OutlineTaskCompletion | null,
  // #217 S6 F4 — the current session's bookmarks (uuid → { note, ts }), client-
  // only. A trailing param (the same extra-param pattern taskCompletion uses) so
  // the section walk is untouched; the bookmark pass runs LAST over the full
  // skeleton. Absent/empty → byte-identical output (no bookmark entries).
  bookmarks?: Record<string, { note: string; ts: number }>,
): DerivedOutline {
  // 1. Bucket sidechains by subagent_key over the WHOLE list (mirror
  //    groupSidechains resolution; placement differs below).
  const buckets = new Map<string, OutlineTurn[]>();
  for (const t of turns) {
    if (t.subagent_key != null) {
      const b = buckets.get(t.subagent_key);
      if (b) b.push(t); else buckets.set(t.subagent_key, [t]);
    }
  }

  // #217 S3 E6(c) — the tree. A subagent bucket whose `parent_subagent_key`
  // points at ANOTHER bucket that EXISTS here nests under that parent (mirroring
  // the reader's groupSidechains recursion); any bucket whose parent is null /
  // absent / unresolved (no matching bucket) is TOP-LEVEL and emits at its own
  // document position, exactly as before. This split is what makes the tree
  // degenerate to the flat list byte-for-byte when nothing genuinely nests.
  const childrenOf = new Map<string, string[]>();   // parent key → child keys (doc order of first member)
  const isNested = new Set<string>();               // buckets that nest under another bucket
  for (const k of buckets.keys()) {
    const pk = subagentMeta?.[k]?.parent_subagent_key;
    if (pk != null && pk !== k && buckets.has(pk)) {
      (childrenOf.get(pk) ?? childrenOf.set(pk, []).get(pk)!).push(k);
      isNested.add(k);
    }
  }

  const entries: OutlineEntry[] = [];
  const sectionByUuid = new Map<string, string>();
  const emittedBucket = new Set<string>();
  const indexOf = new Map(turns.map((t, i) => [t.uuid, i] as const));

  // The current section's prompt uuid (null before the first prompt). Landmarks
  // emit at depth 1 inside a section, depth 0 before the first prompt.
  let sectionUuid: string | null = null;
  // Cursor to the current section's prompt entry, so thinking accrual is O(1)
  // per turn instead of a linear `entries.find` (set when each prompt is pushed;
  // null before the first prompt → pre-prompt thinking is not accrued).
  let sectionPromptEntry: OutlineEntry | null = null;
  const depth = (): 0 | 1 => (sectionUuid != null ? 1 : 0);

  // Map a turn's member uuids → the current section prompt (only inside a
  // section — pre-first-prompt turns map to nothing).
  const mapMembers = (t: OutlineTurn) => {
    if (sectionUuid == null) return;
    for (const u of t.member_uuids) sectionByUuid.set(u, sectionUuid);
  };

  // Emit one subagent bucket entry at `entryDepth`, mapping its members to the
  // current section, then RECURSE into its child buckets one level deeper
  // (depth-first, document order). `parentEntryId` is the parent bucket's
  // entryId (undefined for a top-level bucket → no key written, so the flat
  // shape is byte-identical). The default `entryDepth = depth()` + no parent
  // reproduces the pre-change call for every top-level bucket.
  const emitBucket = (k: string, entryDepth: number = depth(), parentEntryId?: string) => {
    const b = buckets.get(k)!;
    const anyErr = b.some((t) => t.tools?.some((x) => x.is_error));
    // cache-failure-markers spec §4 — a flagged subagent turn flags the BUCKET
    // row (trailing ⚡) rather than nesting a row inside it (keeps the rail
    // shallow). Use the FIRST flagged member's payload for the row's cacheInfo.
    const cfMember = markersEnabled ? b.find((t) => t.cache_failure) : undefined;
    const entry: OutlineEntry = {
      entryId: `sc:${k}`, uuid: b[0].uuid, type: 'subagent',
      // #193 (Codex P2-4): mirror the thread header — prefer the spawning Task
      // description, fall back to `subagent · <kind>` when none is plumbed.
      label: subagentMeta?.[k]?.description ?? `subagent · ${subagentMeta?.[k]?.kind ?? 'agent'}`,
      depth: entryDepth, error: anyErr, plan: false, question: false, thinkingCount: 0,
      cache: cfMember ? true : undefined,
      cacheInfo: cfMember?.cache_failure
        ? { tokens_recreated: cfMember.cache_failure.tokens_recreated, est_wasted_usd: cfMember.cache_failure.est_wasted_usd }
        : undefined,
      toolCount: b.reduce((n, t) => n + (t.tools?.length ?? 0), 0),
      subagentKey: k, subagentKind: subagentMeta?.[k]?.kind,
      turnIndex: indexOf.get(b[0].uuid) ?? 0,
    };
    // #217 S3 E6(c) — only a genuinely-nested bucket carries parentEntryId, so
    // the flat case writes no key (byte-stable degenerate).
    if (parentEntryId !== undefined) entry.parentEntryId = parentEntryId;
    entries.push(entry);
    emittedBucket.add(k);
    // Every bucket member's uuids map to the enclosing section prompt (so a
    // scroll landing on any sidechain fragment resolves to the section).
    if (sectionUuid != null) for (const t of b) for (const u of t.member_uuids) sectionByUuid.set(u, sectionUuid);
    // #217 S3 E6(c) — recurse into child buckets one level deeper, right after
    // their parent, so the tree reads parent→children contiguously.
    for (const ck of childrenOf.get(k) ?? []) {
      if (!emittedBucket.has(ck)) emitBucket(ck, entryDepth + 1, entry.entryId);
    }
  };

  for (const t of turns) {
    // Subagent member turns are handled by the bucket; place the bucket landmark
    // at the FIRST member's document position, then map all member uuids. #217 S3
    // E6(c) — a NESTED bucket is NOT placed here; it is emitted (indented) by its
    // parent's recursion in emitBucket, so we skip it in the main document walk.
    if (t.subagent_key != null) {
      if (!isNested.has(t.subagent_key) && !emittedBucket.has(t.subagent_key)) {
        emitBucket(t.subagent_key);
      }
      continue;
    }

    const err = (t.tools ?? []).some((x) => x.is_error);
    const plan = (t.tools ?? []).some((x) => x.name != null && PLAN_TOOLS.has(x.name));
    const question = (t.tools ?? []).some((x) => x.name != null && QUESTION_TOOLS.has(x.name));
    const heading = t.kind === 'assistant' && HEADING_RE.test(t.label);
    const toolCount = t.tools?.length ?? 0;
    const thinkN = t.thinking?.length ?? 0;

    if (t.kind === 'human') {
      // Open a new section. The prompt is a depth-0 spine entry; thinkingCount
      // is filled by the generic-assistant accrual below as the section runs.
      sectionUuid = t.uuid;
      sectionByUuid.set(t.uuid, t.uuid);
      for (const u of t.member_uuids) sectionByUuid.set(u, t.uuid);
      sectionPromptEntry = {
        entryId: t.uuid, uuid: t.uuid, type: 'human', label: t.label,
        depth: 0, error: false, plan: false, question: false,
        thinkingCount: 0, toolCount, turnIndex: indexOf.get(t.uuid) ?? 0,
      };
      entries.push(sectionPromptEntry);
      continue;
    }

    if (t.kind === 'meta') {
      // #217 S3 F8 — a COMPACTION meta turn (parser stamp meta_kind
      // 'compaction', #191) becomes a navigable 'compaction' landmark at its
      // document position (depth 0 before the first prompt, else depth 1). The
      // turn already renders in the reader — this is landmark + jump only. Every
      // OTHER meta (command/skill/context/notification) emits NO rail row, as
      // before; members are still mapped so a scroll landing resolves the section.
      mapMembers(t);
      if (t.meta_kind === 'compaction') {
        entries.push({
          entryId: t.uuid, uuid: t.uuid, type: 'compaction',
          label: t.label || 'compaction',
          depth: depth(), error: false, plan: false, question: false,
          thinkingCount: 0, toolCount: 0, turnIndex: indexOf.get(t.uuid) ?? 0,
        });
      }
      continue;
    }

    // assistant / tool_result: emit a landmark iff curated. Thinking always
    // accrues to the section prompt regardless of whether a landmark emits.
    if (thinkN > 0 && sectionPromptEntry != null) sectionPromptEntry.thinkingCount += thinkN;
    mapMembers(t);

    // Resolve the landmark type by precedence: error > plan > question > heading.
    let type: OutlineEntry['type'] | null = null;
    let label = '';
    if (err) {
      type = 'error';
      // An errored heading/plan keeps its own label; a bare error turn labels
      // with the failing tool name.
      const failing = (t.tools ?? []).find((x) => x.is_error)?.name;
      label = heading ? t.label : `tool error${failing ? ` · ${failing}` : ''}`;
    } else if (plan) {
      type = 'plan';
      label = heading ? t.label : 'plan';
    } else if (question) {
      type = 'question';
      label = heading ? t.label : 'question';
    } else if (heading) {
      type = 'heading';
      label = t.label;
    }
    // cache-failure-markers spec §4 — the per-turn flag (main-thread only;
    // subagent members never reach here, they're handled by emitBucket above).
    const cf = markersEnabled ? t.cache_failure : undefined;
    const cacheInfo = cf
      ? { tokens_recreated: cf.tokens_recreated, est_wasted_usd: cf.est_wasted_usd }
      : undefined;

    if (type == null) {
      // Generic prose / pure tool relay → normally no row. But a FLAGGED generic
      // turn emits a STANDALONE 'cache' landmark at its document position
      // (spec §4 case 1). Unflagged generic turns still drop out.
      if (cf) {
        entries.push({
          entryId: t.uuid, uuid: t.uuid, type: 'cache', label: cacheLabel(cf),
          depth: depth(), error: false, plan: false, question: false,
          cache: true, cacheInfo,
          thinkingCount: 0, toolCount, turnIndex: indexOf.get(t.uuid) ?? 0,
        });
      }
      continue;
    }

    // A flagged turn that ALSO coincides with a landmark (plan/error/heading)
    // keeps its own type/glyph/label but takes the cache flag + cacheInfo
    // (spec §4 case 2) — a trailing ⚡ suffix, mirroring the thinking suffix.
    entries.push({
      entryId: t.uuid, uuid: t.uuid, type, label,
      depth: depth(), error: err, plan, question,
      cache: cf ? true : undefined, cacheInfo,
      thinkingCount: 0, toolCount, turnIndex: indexOf.get(t.uuid) ?? 0,
    });
  }

  // Defensive sweep: any bucket the main walk never reached (mirrors
  // groupSidechains' final sweep — guarantees no sidechain is silently dropped).
  for (const k of buckets.keys()) if (!emittedBucket.has(k)) emitBucket(k);

  // #217 S5 F7 — a terminal "✓ Session complete (N tasks)" landmark, emitted
  // ONLY when the main thread's final task snapshot is fully done (Q5). It jumps
  // to `anchor_uuid` (the turn carrying that snapshot). Placed last (the session
  // closing marker) at the enclosing section's depth.
  if (taskCompletion?.all_done) {
    const n = taskCompletion.total;
    entries.push({
      entryId: `completion:${taskCompletion.anchor_uuid}`,
      uuid: taskCompletion.anchor_uuid,
      type: 'completion',
      label: `✓ Session complete (${n} task${n === 1 ? '' : 's'})`,
      depth: depth(), error: false, plan: false, question: false,
      thinkingCount: 0, toolCount: 0,
      turnIndex: indexOf.get(taskCompletion.anchor_uuid) ?? turns.length,
    });
  }

  // #217 S6 F4 — bookmark landmarks. Run over the FULL skeleton (independent of
  // the early subagent-member `continue` in the section walk above) so a bookmark
  // on a subagent-member turn still appears (Codex P1). Each entry uses
  // `entryId: bm:<uuid>` (NOT the bare uuid) to avoid a React-key collision with
  // a coinciding prompt/landmark row that keys on the same uuid; its `uuid` stays
  // the jump anchor. A stale bookmark (uuid not in the skeleton — e.g. a turn
  // since compacted away) is skipped. Each bookmark is stable-inserted before the
  // first existing entry with a strictly greater turnIndex, so the existing
  // tree/nesting order is untouched.
  if (bookmarks && Object.keys(bookmarks).length) {
    const idxOf = new Map<string, number>();
    turns.forEach((t, i) => idxOf.set(t.uuid, i));
    const bmEntries: OutlineEntry[] = [];
    for (const t of turns) {
      const bm = bookmarks[t.uuid];
      if (!bm) continue;
      const turnIndex = idxOf.get(t.uuid) ?? 0;
      bmEntries.push({
        entryId: `bm:${t.uuid}`, uuid: t.uuid, type: 'bookmark',
        label: bm.note || t.label || `turn ${turnIndex + 1}`,
        depth: 0, error: false, plan: false, question: false,
        thinkingCount: 0, toolCount: 0, turnIndex,
      });
    }
    for (const bm of bmEntries) {
      let at = entries.length;
      for (let i = 0; i < entries.length; i++) {
        if (entries[i].turnIndex > bm.turnIndex) { at = i; break; }
      }
      entries.splice(at, 0, bm);
    }
  }

  return { entries, sectionByUuid };
}
