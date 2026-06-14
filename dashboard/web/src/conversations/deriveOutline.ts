import type { OutlineTurn, SubagentMeta } from '../types/conversation';

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
  type: 'human' | 'heading' | 'subagent' | 'error' | 'plan' | 'question';
  label: string;
  depth: 0 | 1;                       // 0 = prompt / pre-prompt landmark; 1 = section landmark
  error: boolean;
  plan: boolean;                      // ExitPlanMode present
  question: boolean;                  // AskUserQuestion present
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

  const emitBucket = (k: string) => {
    const b = buckets.get(k)!;
    const anyErr = b.some((t) => t.tools?.some((x) => x.is_error));
    entries.push({
      entryId: `sc:${k}`, uuid: b[0].uuid, type: 'subagent',
      // #193 (Codex P2-4): mirror the thread header — prefer the spawning Task
      // description, fall back to `subagent · <kind>` when none is plumbed.
      label: subagentMeta?.[k]?.description ?? `subagent · ${subagentMeta?.[k]?.kind ?? 'agent'}`,
      depth: depth(), error: anyErr, plan: false, question: false, thinkingCount: 0,
      toolCount: b.reduce((n, t) => n + (t.tools?.length ?? 0), 0),
      subagentKey: k, subagentKind: subagentMeta?.[k]?.kind,
      turnIndex: indexOf.get(b[0].uuid) ?? 0,
    });
    emittedBucket.add(k);
    // Every bucket member's uuids map to the enclosing section prompt (so a
    // scroll landing on any sidechain fragment resolves to the section).
    if (sectionUuid != null) for (const t of b) for (const u of t.member_uuids) sectionByUuid.set(u, sectionUuid);
  };

  for (const t of turns) {
    // Subagent member turns are handled by the bucket; place the bucket landmark
    // at the FIRST member's document position, then map all member uuids.
    if (t.subagent_key != null) {
      if (!emittedBucket.has(t.subagent_key)) emitBucket(t.subagent_key);
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
      // command meta (and skill/context) → no rail row. Still map members so a
      // scroll landing on it resolves to the enclosing section.
      mapMembers(t);
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
    if (type == null) continue; // generic prose / pure tool relay → no row

    entries.push({
      entryId: t.uuid, uuid: t.uuid, type, label,
      depth: depth(), error: err, plan, question,
      thinkingCount: 0, toolCount, turnIndex: indexOf.get(t.uuid) ?? 0,
    });
  }

  // Defensive sweep: any bucket the main walk never reached (mirrors
  // groupSidechains' final sweep — guarantees no sidechain is silently dropped).
  for (const k of buckets.keys()) if (!emittedBucket.has(k)) emitBucket(k);

  return { entries, sectionByUuid };
}
