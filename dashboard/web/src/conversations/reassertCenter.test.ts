// #237 — pure convergent re-center loop tests. All effects are injected (no DOM,
// no real rAF/clock) so convergence/abort/exhaust/target-swap are unit-testable;
// the real DOM measurement + rAF + pixel landing are the Playwright ui-qa gate's.
import { describe, expect, it } from 'vitest';
import { reassertCenter, type ReassertFrame } from './reassertCenter';

// Drive a scripted sequence of measured frames (desired offset + optional target
// identity; null = target gone). Counts apply() calls and advances a fake clock.
function driver(seq: Array<{ desired: number; target?: unknown } | null>, opts?: { msPerFrame?: number }) {
  const msPerFrame = opts?.msPerFrame ?? 16;
  let i = 0;
  let clock = 0;
  const applied: ReassertFrame[] = [];
  const deps = {
    measure: () => {
      const s = seq[Math.min(i, seq.length - 1)];
      return s == null ? null : { desired: s.desired, target: s.target ?? 'M' };
    },
    apply: (f: ReassertFrame) => { applied.push(f); },
    nextFrame: async () => { i++; clock += msPerFrame; },
    now: () => clock,
    isAborted: () => false,
    tol: 1, stableNeeded: 4, budgetMs: 800,
  };
  return { deps, applied: () => applied };
}

describe('reassertCenter', () => {
  it('applies the first frame, re-applies through a collapse, then settles', async () => {
    const seq = [{ desired: 1000 }, { desired: 950 }, { desired: 900 }, { desired: 880 },
      { desired: 880 }, { desired: 880 }, { desired: 880 }, { desired: 880 }];
    const { deps, applied } = driver(seq);
    expect(await reassertCenter(deps)).toBe('settled');
    expect(applied().map((f) => f.desired)).toEqual([1000, 950, 900, 880]); // re-applied each changing frame, not the holds
  });

  it('a 2-frame lull then resume does NOT settle early', async () => {
    const seq = [{ desired: 1000 }, { desired: 1000 }, { desired: 1000 }, { desired: 900 },
      { desired: 900 }, { desired: 900 }, { desired: 900 }, { desired: 900 }];
    const { deps, applied } = driver(seq);
    expect(await reassertCenter(deps)).toBe('settled');
    expect(applied().map((f) => f.desired)).toEqual([1000, 900]); // the resume reset the stable count + re-applied
  });

  it('a target-identity swap resets the stable counter and re-applies', async () => {
    const seq = [{ desired: 500, target: 'A' }, { desired: 500, target: 'A' }, { desired: 500, target: 'A' },
      { desired: 500, target: 'B' }, { desired: 500, target: 'B' }, { desired: 500, target: 'B' },
      { desired: 500, target: 'B' }, { desired: 500, target: 'B' }];
    const { deps, applied } = driver(seq);
    expect(await reassertCenter(deps)).toBe('settled');
    expect(applied().map((f) => f.target)).toEqual(['A', 'B']); // applied once per distinct target run, despite equal desired
  });

  it('returns "gone" when measure() yields null (target recycled mid-loop)', async () => {
    const { deps } = driver([{ desired: 500 }, null]);
    expect(await reassertCenter(deps)).toBe('gone');
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
