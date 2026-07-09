import type { ConversationFilters } from '../types/conversation';

// #217 S4 / I-2.5 — extracted from useConversations.ts so BOTH the browse rail
// (/api/conversations) and search (/api/conversation/search) serialize the
// shared `conversationFilters` state identically (the spec's "one shared filter
// set, auto-applied" decision).
//
// Serialize the active filters into a query-string fragment. Each axis appends a
// parameterized predicate server-side; absent axes are simply omitted.
// `projects` repeats (?projects=a&projects=b) so the server reads the
// multi-select as an IN(...). Returns '' (NOT '&…') when no axis is active so the
// base URL stays byte-identical to the unfiltered path.
export function filterParams(f: ConversationFilters): string {
  const p = new URLSearchParams();
  if (f.dateFrom) p.set('date_from', f.dateFrom);
  if (f.dateTo) p.set('date_to', f.dateTo);
  for (const proj of f.projects) p.append('projects', proj);
  if (f.costMin != null) p.set('cost_min', String(f.costMin));
  if (f.costMax != null) p.set('cost_max', String(f.costMax));
  if (f.rebuildMin != null) p.set('rebuild_min', String(f.rebuildMin));
  // #278 Theme C — repeated ?models= per selected family, absent when empty (so
  // the unfiltered base URL stays byte-identical), mirroring `projects`.
  for (const m of f.models) p.append('models', m);
  const s = p.toString();
  return s ? `&${s}` : '';
}
