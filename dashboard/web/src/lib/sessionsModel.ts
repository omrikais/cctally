import type { SessionRow } from '../types/envelope';
import { abbreviateModel } from './modelName';

// Models that are NOT a real id — any one of these in the set means "do not
// collapse" (keep the per-row model column as-is). '—' is the server's
// degenerate no-model sentinel; '(unknown)' is the unresolved bucket.
const NON_MODEL = new Set(['', '—', '(unknown)']);

// C3: the single-model predicate. Returns the shared ABBREVIATED model label
// (e.g. "opus-4-8") iff EVERY row has the same real model; null for a mixed
// set, an empty set, or any blank/'—'/'(unknown)' row. Computed over the FULL
// session set (never the filtered/paged slice) so a project-filtered
// single-model view still keeps its meaningful per-row model-filter chip.
export function singleModelLabel(rows: SessionRow[]): string | null {
  if (rows.length === 0) return null;
  let model: string | null = null;
  for (const r of rows) {
    const m = (r.model ?? '').trim();
    if (NON_MODEL.has(m)) return null;
    if (model === null) model = m;
    else if (m !== model) return null;
  }
  return model === null ? null : abbreviateModel(model);
}
