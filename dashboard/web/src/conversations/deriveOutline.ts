import type { OutlineTurn, SubagentMeta } from '../types/conversation';

// #177 S5 — pure landmark curation over the full-session outline skeleton (spec
// §2 / Q3). Maps the server's per-turn skeleton to the navigable outline entry
// list the panel renders. Curation policy lives HERE (client side), not in the
// kernel: which turns are landmarks, their labels, their depth, their glyphs.
//
// Sidechain bucketing MIRRORS groupSidechains EXACTLY (read alongside
// conversations/groupSidechains.ts): bucket by subagent_key over the WHOLE list
// (not contiguous runs); a bucket nests iff its first non-meta member's
// parent_uuid resolves to a MAIN turn's MEMBER uuid; non-nested buckets emit one
// entry at the bucket-root document position, nested buckets emit as a depth-1
// child right after the resolved parent's entry; a defensive final sweep emits
// any bucket the main walk never reached. Pure + deterministic.
export interface OutlineEntry {
  uuid: string;                       // jump target (anchor uuid / bucket root)
  type: 'human' | 'assistant' | 'subagent' | 'error' | 'meta';
  label: string;
  depth: 0 | 1;                       // 1 = thinking child / nested subagent
  error: boolean;
  plan: boolean;                      // ExitPlanMode present
  question: boolean;                  // AskUserQuestion present
  toolCount: number;
  subagentKey?: string;
  subagentKind?: string;
  turnIndex: number;                  // skeleton index (cursor math, F4)
}

const PLAN_TOOLS = new Set(['ExitPlanMode']);
const QUESTION_TOOLS = new Set(['AskUserQuestion']);

export function deriveOutline(
  turns: OutlineTurn[],
  subagentMeta: Record<string, SubagentMeta> | undefined,
): OutlineEntry[] {
  // Mirror groupSidechains: bucket sidechains by key; nest iff the bucket's
  // first non-meta member's parent_uuid resolves to a MAIN turn's member uuid.
  const buckets = new Map<string, OutlineTurn[]>();
  const mainByMember = new Map<string, OutlineTurn>();
  for (const t of turns) {
    if (t.subagent_key != null) {
      const b = buckets.get(t.subagent_key);
      if (b) b.push(t); else buckets.set(t.subagent_key, [t]);
    } else {
      for (const u of t.member_uuids) mainByMember.set(u, t);
    }
  }
  const nestedUnder = new Map<string, string[]>();   // main anchor uuid -> keys
  const nested = new Set<string>();
  for (const [k, b] of buckets) {
    const root = b.find((t) => t.kind !== 'meta') ?? b[0];
    const parent = root.parent_uuid != null ? mainByMember.get(root.parent_uuid) : undefined;
    if (parent) {
      nested.add(k);
      const arr = nestedUnder.get(parent.uuid);
      if (arr) arr.push(k); else nestedUnder.set(parent.uuid, [k]);
    }
  }
  const out: OutlineEntry[] = [];
  const emitted = new Set<string>();
  const indexOf = new Map(turns.map((t, i) => [t.uuid, i] as const));
  const subagentEntry = (k: string, depth: 0 | 1) => {
    const b = buckets.get(k)!;
    const anyErr = b.some((t) => t.tools?.some((x) => x.is_error));
    out.push({
      uuid: b[0].uuid, type: 'subagent',
      label: `subagent · ${subagentMeta?.[k]?.kind ?? 'agent'}`,
      depth, error: anyErr, plan: false, question: false,
      toolCount: b.reduce((n, t) => n + (t.tools?.length ?? 0), 0),
      subagentKey: k, subagentKind: subagentMeta?.[k]?.kind,
      turnIndex: indexOf.get(b[0].uuid) ?? 0,
    });
    emitted.add(k);
  };
  for (const t of turns) {
    if (t.subagent_key != null) {
      if (!emitted.has(t.subagent_key) && !nested.has(t.subagent_key)) subagentEntry(t.subagent_key, 0);
      continue;
    }
    const err = (t.tools ?? []).some((x) => x.is_error);
    const plan = (t.tools ?? []).some((x) => x.name != null && PLAN_TOOLS.has(x.name));
    const question = (t.tools ?? []).some((x) => x.name != null && QUESTION_TOOLS.has(x.name));
    const base = {
      uuid: t.uuid, depth: 0 as const, error: err, plan, question,
      toolCount: t.tools?.length ?? 0, turnIndex: indexOf.get(t.uuid) ?? 0,
    };
    if (t.kind === 'human') {
      out.push({ ...base, type: 'human', label: t.label });
    } else if (t.kind === 'assistant') {
      const hasContent = t.label !== '' || (t.thinking?.length ?? 0) > 0;
      if (hasContent || err || plan || question) {
        out.push({ ...base, type: err && t.label === '' ? 'error' : 'assistant',
                   label: t.label || (t.tools?.find((x) => x.is_error)?.name ?? 'tool error') });
        for (const th of t.thinking ?? []) {
          out.push({ ...base, type: 'assistant', label: th, depth: 1, error: false,
                     plan: false, question: false, toolCount: 0 });
        }
      }
    } else if (t.kind === 'tool_result') {
      if (err) out.push({ ...base, type: 'error', label: 'tool error' });
    } else if (t.kind === 'meta' && t.meta_kind === 'command') {
      out.push({ ...base, type: 'meta', label: t.label || t.skill_name || 'command' });
    }
    for (const k of nestedUnder.get(t.uuid) ?? []) if (!emitted.has(k)) subagentEntry(k, 1);
  }
  for (const k of buckets.keys()) if (!emitted.has(k)) subagentEntry(k, 0);
  return out;
}
