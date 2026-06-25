// #237 — convergent re-center loop for an auto-expanded disclosure find-jump.
// A jump-opened thinking/tool <details> settles to a SHORTER height over ~150ms
// (content-visibility / lazy layout) AFTER the open; with scrollTop pinned by a
// single center+re-assert, the content above the matched <mark> collapsing pulls
// the word UP (measured up to ~640px above center on a tall turn). The
// turn-anchored quiesce can't catch it (the turn top never moves, only the mark
// rides up) and false-settles on a 2-frame lull. This loop re-centers the mark
// every frame until the computed center offset stops changing, so the word stays
// pinned at center through the whole collapse and lands dead-center.
//
// No DOM — all effects are injected (mirrors walkToTarget.ts) so the
// convergence/abort/exhaust/target-swap logic is unit-testable; the real DOM
// measurement + rAF + pixel landing are the Playwright ui-qa gate's job.

/** One frame's resolution: the desired center scrollTop + the target's identity. */
export interface ReassertFrame {
  desired: number;
  /** The element measured this frame; compared by identity (Object.is) across
   *  frames so a changed landable mark restarts the stable count. */
  target: unknown;
}

export interface ReassertDeps {
  /** Resolve the current target + its desired center scrollTop. null → target
   *  gone (row recycled / disconnected) → returns 'gone'. */
  measure: () => ReassertFrame | null;
  /** Re-center to the frame `measure` just returned (its `target`). */
  apply: (frame: ReassertFrame) => void;
  /** Resolve after the next animation frame. */
  nextFrame: () => Promise<void>;
  /** Monotonic wall clock (ms) — the cap is time, not frames, so it is
   *  refresh-rate-independent (Codex P2-1). */
  now: () => number;
  /** cancelled || token superseded. */
  isAborted: () => boolean;
  /** px tolerance for "the center offset stopped changing". */
  tol: number;
  /** consecutive same-target within-tol frames that declare a settle. */
  stableNeeded: number;
  /** wall-clock fallback ceiling in ms. */
  budgetMs: number;
}

export type ReassertResult = 'settled' | 'aborted' | 'exhausted' | 'gone';

/** Re-center every frame until the center offset stabilizes (spec §2). */
export async function reassertCenter(d: ReassertDeps): Promise<ReassertResult> {
  const start = d.now();
  let last: ReassertFrame | null = null;
  let stable = 0;
  while (d.now() - start <= d.budgetMs) {
    if (d.isAborted()) return 'aborted';
    const cur = d.measure();
    if (cur == null) return 'gone';
    const prev = last; // narrow once so the same-target/within-tol test needs no non-null assertion
    if (prev && Object.is(prev.target, cur.target) && Math.abs(cur.desired - prev.desired) <= d.tol) {
      if (++stable >= d.stableNeeded) return 'settled';
    } else {
      stable = 0;
      d.apply(cur);
    }
    last = cur;
    await d.nextFrame();
  }
  return 'exhausted';
}
