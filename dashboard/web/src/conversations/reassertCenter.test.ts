// #237 — pure convergent re-center loop tests. All effects are injected (no DOM,
// no real rAF/clock) so convergence/abort/exhaust/target-swap are unit-testable;
// the real DOM measurement + rAF + pixel landing are the Playwright ui-qa gate's.
import { describe, expect, it } from 'vitest';
import { reassertCenter, type ReassertFrame } from './reassertCenter';

// Drive a scripted sequence of measured frames (desired offset + optional target
// identity; null = target gone). Counts apply() calls and advances a fake clock.
function driver(
  seq: Array<{ desired: number; target?: unknown } | null>,
  opts?: { msPerFrame?: number; transientGoneGraceMs?: number },
) {
  const msPerFrame = opts?.msPerFrame ?? 16;
  let i = 0;
  let clock = 0;
  let nextFrameCount = 0;
  const applied: ReassertFrame[] = [];
  const deps = {
    measure: () => {
      const s = seq[Math.min(i, seq.length - 1)];
      return s == null ? null : { desired: s.desired, target: s.target ?? 'M' };
    },
    apply: (f: ReassertFrame) => { applied.push(f); },
    nextFrame: async () => { i++; clock += msPerFrame; nextFrameCount++; },
    now: () => clock,
    isAborted: () => false,
    tol: 1, stableNeeded: 4, budgetMs: 800,
    transientGoneGraceMs: opts?.transientGoneGraceMs,
  };
  return { deps, applied: () => applied, nextFrames: () => nextFrameCount };
}

describe('reassertCenter', () => {
  it('applies the first frame, re-applies through a collapse, then settles', async () => {
    const seq = [{ desired: 1000 }, { desired: 950 }, { desired: 900 }, { desired: 880 },
      { desired: 880 }, { desired: 880 }, { desired: 880 }, { desired: 880 }];
    const { deps, applied } = driver(seq);
    expect(await reassertCenter(deps)).toBe('settled');
    // #237 — re-centers EVERY frame (the collapse AND the stable tail), so the last
    // apply lands on the settled layout; settles after stableNeeded stable frames.
    expect(applied().map((f) => f.desired)).toEqual([1000, 950, 900, 880, 880, 880, 880, 880]);
  });

  it('#237 — re-centers on within-tol (stable) frames too, so a sub-pixel collapse tail stays tracked', async () => {
    // The disclosure collapse decays via a sub-pixel tail where each frame moves
    // <= tol. An apply-on-change-only loop would re-apply only on frame 0 and let
    // that tail creep the mark ~30px uncorrected (the gate-found residual); applying
    // every frame re-centers through the creep. This test fails on apply-on-change
    // (applied would be just [1000]).
    const seq = [{ desired: 1000 }, { desired: 999 }, { desired: 998 }, { desired: 998 },
      { desired: 998 }, { desired: 998 }, { desired: 998 }];
    const { deps, applied } = driver(seq);
    expect(await reassertCenter(deps)).toBe('settled');
    expect(applied().map((f) => f.desired)).toContain(999); // a within-tol frame WAS re-applied
    expect(applied().map((f) => f.desired)).toContain(998);
    expect(applied().length).toBeGreaterThanOrEqual(5);
  });

  it('a 2-frame lull then resume does NOT settle early', async () => {
    const seq = [{ desired: 1000 }, { desired: 1000 }, { desired: 1000 }, { desired: 900 },
      { desired: 900 }, { desired: 900 }, { desired: 900 }, { desired: 900 }];
    const { deps, applied } = driver(seq);
    expect(await reassertCenter(deps)).toBe('settled');
    // the lull's three 1000s only reach stable=2, then the 900 resets; settles only
    // after four stable 900s — re-centering every frame throughout.
    expect(applied().map((f) => f.desired)).toEqual([1000, 1000, 1000, 900, 900, 900, 900, 900]);
  });

  it('a target-identity swap resets the stable counter and re-applies', async () => {
    const seq = [{ desired: 500, target: 'A' }, { desired: 500, target: 'A' }, { desired: 500, target: 'A' },
      { desired: 500, target: 'B' }, { desired: 500, target: 'B' }, { desired: 500, target: 'B' },
      { desired: 500, target: 'B' }, { desired: 500, target: 'B' }];
    const { deps, applied } = driver(seq);
    expect(await reassertCenter(deps)).toBe('settled');
    // the A-run reaches only stable=2 before the identity swap to B resets it (despite
    // equal desired); settles only after four stable B frames.
    expect(applied().map((f) => f.target)).toEqual(['A', 'A', 'A', 'B', 'B', 'B', 'B', 'B']);
  });

  it('returns "gone" when measure() yields null (target recycled mid-loop)', async () => {
    // No `transientGoneGraceMs` opt-in → default 0 → immediate 'gone' on the first
    // null. This is the SidechainGroup / #239 contract and MUST stay unchanged.
    const { deps } = driver([{ desired: 500 }, null]);
    expect(await reassertCenter(deps)).toBe('gone');
  });

  it('#291 — with transientGoneGraceMs set, a transient recycle is tolerated and the loop resumes centering', async () => {
    // Virtuoso's deferred ResizeObserver re-measure transiently UNMOUNTS the target
    // row (~57ms) on a force-open (Disruption C). With an opt-in per-outage grace,
    // reassertCenter keeps awaiting frames across the null gap and resumes centering
    // the instant the row re-mounts — instead of bailing 'gone'. `last`/`stable`
    // reset on each null frame, so stability never bridges the unmounted interval
    // (apply skips the null frames: [a, b, b, b, b, b], never a null).
    const a = { desired: 1000 };
    const b = { desired: 500 };
    const { deps, applied } = driver(
      [a, null, null, null, b, b, b, b, b],
      { transientGoneGraceMs: 100 },
    );
    expect(await reassertCenter(deps)).toBe('settled');
    expect(applied().map((f) => f.desired)).toEqual([1000, 500, 500, 500, 500, 500]);
  });

  it('#291 — the grace is PER-OUTAGE (resets on every successful measure), not a first-null lifetime clock', async () => {
    // Two separate recycles, each shorter than the grace, but the total span from the
    // FIRST null exceeds it. A first-null-lifetime clock would return 'gone' at the
    // second outage (128-16=112 >= 100); resetting `goneSince` on every successful
    // measure keeps each outage under budget → 'settled'. Proves per-outage grace.
    const A = { desired: 1000 };
    const B = { desired: 800 };
    const C = { desired: 600 };
    const { deps, applied } = driver(
      [A, null, null, null, null, null, null, B, null, null, null, C, C, C, C],
      { transientGoneGraceMs: 100 },
    );
    expect(await reassertCenter(deps)).toBe('settled');
    expect(applied().map((f) => f.desired)).toEqual([1000, 800, 600, 600, 600, 600, 600]);
  });

  it('#291 — a GENUINE gone (null past the grace) still returns "gone" (non-vacuous: waits > 1)', async () => {
    // The target never re-mounts; once the per-outage grace elapses (relative 112 >=
    // 100 at clock 128) the loop returns 'gone'. apply fired once (the initial frame);
    // nextFrame was awaited MORE than once — an immediate-exit impl bails after the
    // first null (waits === 1), so `waits > 1` proves the grace loop actually iterated.
    const { deps, applied, nextFrames } = driver(
      [{ desired: 1000 }, null, null, null, null, null, null, null, null, null],
      { transientGoneGraceMs: 100 },
    );
    expect(await reassertCenter(deps)).toBe('gone');
    expect(applied().length).toBe(1);
    expect(nextFrames()).toBeGreaterThan(1);
  });

  it('returns "exhausted" when the wall-clock budget elapses before settling', async () => {
    const ever = Array.from({ length: 200 }, (_, k) => ({ desired: 1000 - 2 * k })); // changes >tol every frame → never stable
    const { deps } = driver(ever, { msPerFrame: 100 }); // 800ms budget → exits ~frame 9
    expect(await reassertCenter(deps)).toBe('exhausted');
  });

  it('returns "aborted" when isAborted() flips', async () => {
    let aborted = false;
    const deps = {
      measure: () => ({ desired: 1, target: 'M' }),
      apply: () => {},
      nextFrame: async () => { aborted = true; },
      now: () => 0, isAborted: () => aborted,
      tol: 1, stableNeeded: 4, budgetMs: 800,
    };
    expect(await reassertCenter(deps)).toBe('aborted');
  });
});
