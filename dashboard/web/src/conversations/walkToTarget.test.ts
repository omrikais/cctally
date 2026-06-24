// #234 — pure walk-planner + orchestrator tests. The orchestrator injects all
// effects (no DOM), so its convergence/abort/exhaust logic is unit-testable;
// pixel-exact landing under react-virtuoso stays the Playwright ui-qa gate's job.
import { describe, expect, it } from 'vitest';
import { planWalkStep, nextStepSize, walkToTarget } from './walkToTarget';

describe('planWalkStep', () => {
  it('done when target is within the mounted range', () => {
    expect(planWalkStep({ targetArrayIndex: 5, first: 3, last: 8, step: 10 })).toEqual({ kind: 'done' });
  });
  it('steps down toward a target below, clamped not to overshoot', () => {
    expect(planWalkStep({ targetArrayIndex: 100, first: 0, last: 9, step: 10 }))
      .toEqual({ kind: 'step', index: 19, align: 'end', step: 10 });
    expect(planWalkStep({ targetArrayIndex: 12, first: 0, last: 9, step: 10 }))
      .toEqual({ kind: 'step', index: 12, align: 'end', step: 10 });
  });
  it('steps up toward a target above, clamped not to overshoot', () => {
    expect(planWalkStep({ targetArrayIndex: 0, first: 50, last: 60, step: 10 }))
      .toEqual({ kind: 'step', index: 40, align: 'start', step: 10 });
    expect(planWalkStep({ targetArrayIndex: 45, first: 50, last: 60, step: 10 }))
      .toEqual({ kind: 'step', index: 45, align: 'start', step: 10 });
  });
  it('floors the step at 1', () => {
    expect(planWalkStep({ targetArrayIndex: 100, first: 0, last: 9, step: 0 }))
      .toEqual({ kind: 'step', index: 10, align: 'end', step: 1 });
  });
});

describe('nextStepSize', () => {
  it('resets to a full window on contiguous progress', () => {
    expect(nextStepSize(2, true, 12)).toBe(12);
  });
  it('halves (>=1) on stall', () => {
    expect(nextStepSize(12, false, 12)).toBe(6);
    expect(nextStepSize(1, false, 12)).toBe(1);
  });
});

function fakeVirtuoso(rowsPerWindow: number, total: number) {
  // a fake where scrollToIndex(i) mounts a window [i .. i+rowsPerWindow-1]
  // clamped into [0, total-1]; tracks calls. Starts at the tail.
  let first = total - rowsPerWindow, last = total - 1; // start at tail
  const calls: number[] = [];
  return {
    getMountedArrayRange: () => ({ first, last }),
    scrollToIndex: (index: number) => {
      calls.push(index);
      first = Math.max(0, Math.min(index, total - rowsPerWindow));
      last = Math.min(total - 1, first + rowsPerWindow - 1);
    },
    calls,
  };
}

describe('walkToTarget', () => {
  it('converges to a far target above the tail and reports mounted', async () => {
    const fv = fakeVirtuoso(10, 400);
    const res = await walkToTarget({
      getTargetArrayIndex: () => 0,
      getMountedArrayRange: fv.getMountedArrayRange,
      scrollToIndex: fv.scrollToIndex,
      quiesce: async () => {},
      isAborted: () => false,
      maxSteps: 100,
      initialWindow: 10,
    });
    expect(res).toBe('mounted');
    const r = fv.getMountedArrayRange();
    expect(0 >= r.first && 0 <= r.last).toBe(true);
  });

  it('aborts immediately when superseded', async () => {
    const fv = fakeVirtuoso(10, 400);
    const res = await walkToTarget({
      getTargetArrayIndex: () => 0, getMountedArrayRange: fv.getMountedArrayRange,
      scrollToIndex: fv.scrollToIndex, quiesce: async () => {}, isAborted: () => true,
      maxSteps: 100, initialWindow: 10,
    });
    expect(res).toBe('aborted');
    expect(fv.calls.length).toBe(0);
  });

  it('returns exhausted if it cannot make progress within maxSteps', async () => {
    // a stuck fake: scrollToIndex never moves the range
    const res = await walkToTarget({
      getTargetArrayIndex: () => 0,
      getMountedArrayRange: () => ({ first: 50, last: 60 }),
      scrollToIndex: () => {}, quiesce: async () => {}, isAborted: () => false,
      maxSteps: 8, initialWindow: 10,
    });
    expect(res).toBe('exhausted');
  });

  it('returns mounted immediately if the target is already in range', async () => {
    const res = await walkToTarget({
      getTargetArrayIndex: () => 55, getMountedArrayRange: () => ({ first: 50, last: 60 }),
      scrollToIndex: () => { throw new Error('should not step'); }, quiesce: async () => {},
      isAborted: () => false, maxSteps: 8, initialWindow: 10,
    });
    expect(res).toBe('mounted');
  });

  it('aborts if the target can no longer be resolved (node trimmed)', async () => {
    const res = await walkToTarget({
      getTargetArrayIndex: () => null, getMountedArrayRange: () => ({ first: 50, last: 60 }),
      scrollToIndex: () => {}, quiesce: async () => {}, isAborted: () => false,
      maxSteps: 8, initialWindow: 10,
    });
    expect(res).toBe('aborted');
  });
});
