import type { ConversationSummary, SearchHit, SearchKind } from '../types/conversation';

// True when every loaded row shares one project_label (≤1 distinct). The browse
// list uses this to suppress the per-row project label on single-project
// installs; an empty list is vacuously "one project" (no rows to disambiguate).
export function allOneProject(rows: ConversationSummary[]): boolean {
  const seen = new Set<string>();
  for (const r of rows) {
    seen.add(r.project_label);
    if (seen.size > 1) return false;
  }
  return true;
}

type Badge = NonNullable<SearchHit['match_kinds']>[number];

// The badge a single-kind facet would merely echo (so it says nothing the facet
// header doesn't). 'all'/'prompts'/'assistant' map to nothing → no suppression.
const FACET_ECHO: Partial<Record<SearchKind, Badge>> = {
  tools: 'tool', thinking: 'thinking', title: 'title', files: 'file',
};

// Filter the RENDERED badge group: drop the facet-echoing badge in a single-kind
// view; keep every badge in the multi-kind All view. NOTE: callers must keep the
// RAW hit.match_kinds for behavioral checks (e.g. file-hit layout) — this governs
// display only.
export function visibleBadges(badges: Badge[], activeKind: SearchKind): Badge[] {
  const echo = FACET_ECHO[activeKind];
  return echo ? badges.filter((b) => b !== echo) : badges;
}
