// #234 — pure walk-planner + orchestrator tests. The orchestrator injects all
// effects (no DOM), so its convergence/abort/exhaust logic is unit-testable;
// pixel-exact landing under react-virtuoso stays the Playwright ui-qa gate's job.
import { describe, expect, it } from 'vitest';
import { planWalkStep, nextStepSize } from './walkToTarget';

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
