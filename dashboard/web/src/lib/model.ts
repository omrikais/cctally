import { abbreviateModel } from './modelName';

// #244 — model families that get a dedicated chip colour. `fable` joins the
// original three (Fable is a current first-class model, e.g. claude-fable-5),
// and `other` is the neutral bucket for genuinely-unrecognized ids (gpt-*, the
// internal <synthetic> placeholder, future models) — they MUST NOT borrow the
// `sonnet` identity (the pre-#244 default silently rendered every unknown model
// as a green "sonnet" chip; the rail chip text IS the class name, so the label
// was actively wrong, not just mis-coloured).
export type ModelChipClass = 'opus' | 'sonnet' | 'haiku' | 'fable' | 'other';

export function modelChipClass(m: string | null | undefined): ModelChipClass {
  if (!m) return 'other';
  if (m.includes('opus')) return 'opus';
  if (m.includes('sonnet')) return 'sonnet';
  if (m.includes('haiku')) return 'haiku';
  if (m.includes('fable')) return 'fable';
  return 'other';
}

// #304 S3 (Codex F4) — the deterministic bound on an `other` chip's DISPLAY
// label. The rail's two-line stats line is rigid (no shrink valve), so an
// arbitrarily long internal/future model id must not be allowed to grow the
// line and push $cost/msgs off the rail. Known-family labels are short and
// unaffected; the bound only clamps the `other` bucket's abbreviation.
export const OTHER_CHIP_LABEL_MAX = 12;

// One rail chip: the colour class (`cls`) plus the SHORT label rendered as the
// chip's text. For a known family the label is the family name (the compact,
// rigid pill the #243 rail layout depends on — "opus"/"sonnet"/"haiku"/
// "fable"). An `other` chip keeps its OWN identity via an abbreviation of the
// real model id (e.g. "gpt-5") rather than the meaningless literal word
// "other" — and that abbreviation is now deterministically BOUNDED for display
// (Codex F4), with the untruncated form carried on `full` for the chip's title
// and accessible name.
export interface ModelChip {
  cls: ModelChipClass;
  label: string;
  // #304 S3 (Codex F4) — the untruncated label for the chip's title/accessible
  // name. Equals `label` for known families and short `other` ids; differs only
  // when an `other` abbreviation exceeded OTHER_CHIP_LABEL_MAX and was clamped.
  full: string;
}

export interface ModelChipSummary {
  chips: ModelChip[];
  extra: number;
}

// Dedupe a session's model strings to unique chip-classes (preserving the
// array's order — `models` is the backend's main-session-first sorted-distinct
// list, NOT a recency/frequency ranking), capped at `cap` with the remainder as
// `extra`. The label of an `other` chip comes from the FIRST model that mapped
// to it (rare edge: two distinct unrecognized ids collapse to one chip — fine
// for the rail, which shows the single primary model; the reader header lists
// every model in full via abbreviateModel).
export function modelChipSummary(models: string[], cap = 2): ModelChipSummary {
  const seen = new Set<ModelChipClass>();
  const chips: ModelChip[] = [];
  for (const m of models) {
    const cls = modelChipClass(m);
    if (seen.has(cls)) continue;
    seen.add(cls);
    // #304 S3 (Codex F4) — `full` is the untruncated label; `label` is the
    // display text, bounded for `other` ids that exceed OTHER_CHIP_LABEL_MAX.
    const full = cls === 'other' ? abbreviateModel(m) : cls;
    const label = cls === 'other' && full.length > OTHER_CHIP_LABEL_MAX
      ? `${full.slice(0, OTHER_CHIP_LABEL_MAX)}…`
      : full;
    chips.push({ cls, label, full });
  }
  return { chips: chips.slice(0, cap), extra: Math.max(0, chips.length - cap) };
}
