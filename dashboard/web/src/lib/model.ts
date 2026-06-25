export type ModelChipClass = 'opus' | 'haiku' | 'sonnet';

export function modelChipClass(m: string | null | undefined): ModelChipClass {
  if (!m) return 'sonnet';
  if (m.includes('opus')) return 'opus';
  if (m.includes('haiku')) return 'haiku';
  return 'sonnet';
}

export interface ModelChipSummary {
  classes: ModelChipClass[];
  extra: number;
}

// Dedupe a session's model strings to unique chip-classes (preserving the
// array's order — `models` is the backend's sorted-distinct list, NOT a
// recency/frequency ranking), capped at `cap` with the remainder as `extra`.
export function modelChipSummary(models: string[], cap = 2): ModelChipSummary {
  const seen = new Set<ModelChipClass>();
  const unique: ModelChipClass[] = [];
  for (const m of models) {
    const c = modelChipClass(m);
    if (!seen.has(c)) { seen.add(c); unique.push(c); }
  }
  return { classes: unique.slice(0, cap), extra: Math.max(0, unique.length - cap) };
}
