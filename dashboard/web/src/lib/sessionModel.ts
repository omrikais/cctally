import type { SessionDetail } from '../types/envelope';

// SE-2 — a session is "single-model" iff exactly one DISTINCT model name spans
// its `models` chip list AND its `cost_per_model` rows (the union). A
// single-model session's "Models" (one chip) + "Cost by model" (a 100% bar +
// one legend row) are two degenerate sections; the Session modal collapses
// them into one caption when this returns true. Zero models → false (the
// existing empty-guards suppress both sections already).
export function isSingleModel(detail: SessionDetail): boolean {
  const names = new Set<string>();
  (detail.models ?? []).forEach((m) => names.add(m.name));
  (detail.cost_per_model ?? []).forEach((c) => names.add(c.model));
  return names.size === 1;
}
