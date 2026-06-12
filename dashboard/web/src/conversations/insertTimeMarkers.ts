import type { FilteredNode } from './applyFocusMode';
import type { FmtCtx } from '../lib/fmt';

// #177 S5 §6 — inter-turn time markers. A pure pass over the FILTERED node list
// (run AFTER applyFocusMode so markers recompute over whatever the active focus
// mode leaves visible). Inserts a `time_marker` between two adjacent timestamped
// nodes when they are ≥ 10 minutes apart (gap marker) and/or the calendar day
// (in ctx.tz) changes (day marker); both → a combined marker. Null-ts nodes and
// hidden_run markers are transparent: they never emit a marker and never break
// the chain — the gap is computed across them from the last timestamped node.
export type TimedNode =
  | FilteredNode
  | { kind: 'time_marker'; gapSeconds: number | null; dayLabel: string | null; key: string };

const GAP_THRESHOLD_S = 600; // ≥ 10 minutes (spec §6)

// The anchor ts for a filtered node; null when the node carries no instant.
// `ts` is nullable on every item kind (Codex F6) — a null-ts node is skipped by
// the marker chain entirely. hidden_run markers never carry an instant.
function nodeTs(n: FilteredNode): string | null {
  if (n.kind === 'hidden_run') return null;
  if (n.kind === 'item') return n.item.ts ?? null;
  // subagent / tool_result_run: anchor on the first member item.
  return n.items[0]?.ts ?? null;
}

export function insertTimeMarkers(nodes: FilteredNode[], ctx: FmtCtx): TimedNode[] {
  const out: TimedNode[] = [];
  let prevTs: Date | null = null;
  // Hoisted once per call (vs once per node-pair) — the formatter is the only
  // allocation worth memoizing here.
  const dayFmt = new Intl.DateTimeFormat('en-US', {
    timeZone: ctx.tz,
    month: 'short',
    day: '2-digit',
  });
  const dayOf = (d: Date) => dayFmt.format(d);
  for (const n of nodes) {
    const iso = nodeTs(n);
    const d = iso ? new Date(iso) : null;
    const valid = d != null && !isNaN(d.getTime());
    if (valid && prevTs) {
      const gap = (d.getTime() - prevTs.getTime()) / 1000;
      const dayChanged = dayOf(d) !== dayOf(prevTs);
      // Negative (out-of-order) gaps emit nothing; a same-day backwards step is
      // never a marker, and a day-changed predicate on a backwards step would be
      // a false positive — guard the whole emit on a non-negative gap.
      if (gap >= 0 && (gap >= GAP_THRESHOLD_S || dayChanged)) {
        // #184 — fold the output position into the React key. A non-monotonic
        // transcript can repeat the SAME instant (resumed/merged sessions,
        // out-of-order writes), so `tm-${iso}` alone could collide across two
        // markers. `out.length` at push time is unique per emitted node, so the
        // composite key stays stable AND collision-free.
        out.push({
          kind: 'time_marker',
          gapSeconds: gap >= GAP_THRESHOLD_S ? gap : null,
          dayLabel: dayChanged ? dayOf(d) : null,
          key: `tm-${out.length}-${iso}`,
        });
      }
    }
    if (valid) prevTs = d; // null-ts / hidden_run neighbors leave the anchor intact
    out.push(n);
  }
  return out;
}
