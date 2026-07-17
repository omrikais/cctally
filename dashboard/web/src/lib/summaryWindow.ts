import type { BoardMode } from './boardLayout';

// #293 S3 — how many newest rows a stacked Weekly/Monthly card previews before
// deferring the rest to its full-table modal. Rows are newest-first, so this is
// the current period + (CAP-1) most-recent emitted priors.
export const SUMMARY_WINDOW_CAP = 3;

export interface SummaryWindow<T> {
  visible: readonly T[];
  hiddenCount: number;
}

// Stack mode → preview the newest CAP rows + how many are deferred.
// Intermediate/bento → ALL rows (the bento inner-scroll bounds them).
export function summarize<T>(rows: readonly T[], mode: BoardMode): SummaryWindow<T> {
  if (mode !== 'stack') return { visible: rows, hiddenCount: 0 };
  return {
    visible: rows.slice(0, SUMMARY_WINDOW_CAP),
    hiddenCount: Math.max(0, rows.length - SUMMARY_WINDOW_CAP),
  };
}
