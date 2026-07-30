import { abbreviateModel } from './modelName';
import {
  createModelColorAllocator,
  logicalModelKey,
  type ModelColorStyle,
} from './modelColor';

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

const dashboardModelColors = createModelColorAllocator();

/** Session-stable exact-model color shared by every dashboard surface. */
export function modelChipStyle(
  model: string | null | undefined,
): ModelColorStyle | undefined {
  return dashboardModelColors.styleFor(model);
}

// #304 S3 (Codex F4) — the deterministic bound on a model chip's DISPLAY
// label. The rail's two-line stats line is rigid (no shrink valve), so an
// arbitrarily long internal/future model id must not be allowed to grow the
// line and push $cost/msgs off the rail.
export const OTHER_CHIP_LABEL_MAX = 12;

// One rail chip: the fallback family colour class (`cls`) plus the logical
// release label rendered as text. Every model keeps its own identity via an
// abbreviation of the real model id (for example "opus-4-8" or "gpt-5")
// rather than a family-only label or the meaningless literal word "other".
// The abbreviation is deterministically BOUNDED for display (Codex F4), with
// the untruncated form carried on `full` for the chip's title and accessible
// name.
export interface ModelChip {
  cls: ModelChipClass;
  label: string;
  // #304 S3 (Codex F4) — the untruncated label for the chip's title/accessible
  // name. Equals `label` for short ids and differs when the abbreviation
  // exceeded OTHER_CHIP_LABEL_MAX and was clamped.
  full: string;
  // Canonical input carried through so every consumer resolves the same
  // logical-model color instead of falling back to the family class.
  model: string;
}

export interface ModelChipSummary {
  chips: ModelChip[];
  extra: number;
}

// Dedupe a session's model strings to logical releases (preserving the array's
// order — `models` is the backend's main-session-first sorted-distinct list,
// NOT a recency/frequency ranking), capped at `cap` with the remainder as
// `extra`. Dated/capacity aliases collapse, while Opus 4.8 and Opus 5 remain
// separate even though both retain the `opus` family class as a CSS fallback.
export function modelChipSummary(models: string[], cap = 2): ModelChipSummary {
  const seen = new Set<string>();
  const chips: ModelChip[] = [];
  for (const model of models) {
    const key = logicalModelKey(model);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    const cls = modelChipClass(model);
    // #304 S3 (Codex F4) — `full` is the untruncated logical-release label;
    // `label` is bounded so any future provider id stays inside the rigid rail.
    const full = abbreviateModel(model).replace(/\[[^\]]+\]$/, '');
    const label = full.length > OTHER_CHIP_LABEL_MAX
      ? `${full.slice(0, OTHER_CHIP_LABEL_MAX)}…`
      : full;
    chips.push({ cls, label, full, model });
  }
  return { chips: chips.slice(0, cap), extra: Math.max(0, chips.length - cap) };
}
