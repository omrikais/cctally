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
   *  gone (row recycled / disconnected). By default a null returns 'gone'
   *  immediately; an opt-in `transientGoneGraceMs` tolerates a transient null
   *  (see below) and resumes centering when the row re-mounts. */
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
  /** #291 — opt-in tolerance (ms) for a TRANSIENT null `measure()`. Virtuoso's
   *  deferred ResizeObserver re-measure transiently recycles the target row
   *  (~57ms) on a force-open (Disruption C), which would otherwise trip the
   *  genuine-`'gone'` exit before the row re-mounts. When set (>0), a null frame
   *  keeps awaiting `nextFrame()` and re-measuring — resuming centering the
   *  instant the row re-mounts — until either it re-mounts or this grace elapses
   *  with the target still gone (then `'gone'`). Grace is measured PER-OUTAGE
   *  (reset on every successful measure), because virtuoso can recycle the row
   *  more than once per operation; the outer `budgetMs` still bounds total time.
   *  Default `0`/unset = today's immediate-`'gone'` behavior (SidechainGroup). */
  transientGoneGraceMs?: number;
}

export type ReassertResult = 'settled' | 'aborted' | 'exhausted' | 'gone';

/** Re-center every frame until the center offset stabilizes (spec §2). */
export async function reassertCenter(d: ReassertDeps): Promise<ReassertResult> {
  const start = d.now();
  const goneGraceMs = Math.max(0, d.transientGoneGraceMs ?? 0);
  let goneSince: number | null = null;   // #291 — per-outage null clock (reset on every successful measure)
  let last: ReassertFrame | null = null;
  let stable = 0;
  while (d.now() - start <= d.budgetMs) {
    if (d.isAborted()) return 'aborted';
    const cur = d.measure();
    if (cur == null) {
      // #291 — tolerate a TRANSIENT recycle when opted in: keep polling for the
      // row's re-mount until a PER-OUTAGE grace elapses; the outer budget still
      // bounds the total. Never `apply` on a null frame; reset `last`/`stable` so
      // stability can never bridge an unmounted interval.
      if (goneGraceMs === 0) return 'gone';                 // default — unchanged (SidechainGroup)
      const now = d.now();
      if (goneSince == null) goneSince = now;
      if (now - goneSince >= goneGraceMs) return 'gone';
      last = null; stable = 0;
      await d.nextFrame();
      continue;
    }
    goneSince = null;                                        // a successful measure resets the per-outage clock
    // #237 — re-center EVERY frame, not just when `desired` moves > tol. A
    // jump-opened disclosure collapses to its settled height via a sub-pixel tail
    // (each frame's shift is ≤ tol), so an apply-on-change-only loop stops
    // re-centering once `desired` reads "stable" while the tail keeps creeping the
    // mark up — measured ~30–38px of uncorrected residual, vs a single fresh
    // center on the settled layout landing it at 0. Applying every frame keeps the
    // word pinned through the tail; the stable counter only decides WHEN to exit.
    d.apply(cur);
    const prev = last; // narrow once so the same-target/within-tol test needs no non-null assertion
    if (prev && Object.is(prev.target, cur.target) && Math.abs(cur.desired - prev.desired) <= d.tol) {
      if (++stable >= d.stableNeeded) return 'settled';
    } else {
      stable = 0;
    }
    last = cur;
    await d.nextFrame();
  }
  return 'exhausted';
}
