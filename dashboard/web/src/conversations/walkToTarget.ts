// #234 §2.2 — the coverage-driven walk that warms react-virtuoso's size model
// before the precision landing. A single estimate-based hop over hundreds of
// unmeasured high-variance rows cannot land deterministically (the measured R1
// failure: scrollHeight corrects 15705→~74266 mid-flight, stranding the target).
// The walk steps Virtuoso toward the target in mounted-window-sized increments,
// re-measuring the mounted range each step, until the target is mounted; then
// the caller direct-centers via scrollNodeIntoView.
export interface WalkState {
  targetArrayIndex: number;
  first: number;  // mounted array-range start
  last: number;   // mounted array-range end
  step: number;   // items to advance this step
}
export type WalkPlan =
  | { kind: 'done' }
  | { kind: 'step'; index: number; align: 'start' | 'end'; step: number };

/** Pure: next scrollToIndex target (array index) toward the goal, clamped not to overshoot (spec §2.2-2). */
export function planWalkStep(s: WalkState): WalkPlan {
  if (s.targetArrayIndex >= s.first && s.targetArrayIndex <= s.last) return { kind: 'done' };
  const step = Math.max(1, Math.floor(s.step));
  if (s.targetArrayIndex > s.last) {
    return { kind: 'step', index: Math.min(s.last + step, s.targetArrayIndex), align: 'end', step };
  }
  return { kind: 'step', index: Math.max(s.first - step, s.targetArrayIndex), align: 'start', step };
}

/** Pure: full window on contiguous progress, else halve — the coverage shrink (spec §2.2-2). */
export function nextStepSize(prevStep: number, progressed: boolean, windowSize: number): number {
  return progressed ? Math.max(1, windowSize) : Math.max(1, Math.floor(prevStep / 2));
}

export interface WalkDeps {
  /** Re-resolve the target's ARRAY index each iteration; null → target gone (abort). */
  getTargetArrayIndex: () => number | null;
  /** Current mounted ARRAY range (renderedRangeRef virtual range minus firstItemIndex). */
  getMountedArrayRange: () => { first: number; last: number };
  /** virtuosoRef.scrollToIndex({ index, align, behavior:'auto' }) — array index only. */
  scrollToIndex: (index: number, align: 'start' | 'end') => void;
  /** Resolve once the layout tuple is stable (spec §2.2-3); see layoutStable.ts in the caller. */
  quiesce: () => Promise<void>;
  /** cancelled || token superseded (spec §2.2-1 / P0-3). */
  isAborted: () => boolean;
  maxSteps: number;
  initialWindow: number;
}

/** Walk Virtuoso toward the target until it is mounted (spec §2.2). No DOM — all effects injected. */
export async function walkToTarget(d: WalkDeps): Promise<'mounted' | 'aborted' | 'exhausted'> {
  if (d.isAborted()) return 'aborted';
  let step = Math.max(1, d.initialWindow);
  for (let i = 0; i < d.maxSteps; i++) {
    if (d.isAborted()) return 'aborted';
    const target = d.getTargetArrayIndex();
    if (target == null) return 'aborted';
    const range = d.getMountedArrayRange();
    const window = Math.max(1, range.last - range.first + 1);
    const plan = planWalkStep({ targetArrayIndex: target, first: range.first, last: range.last, step });
    if (plan.kind === 'done') return 'mounted';
    d.scrollToIndex(plan.index, plan.align);
    await d.quiesce();
    if (d.isAborted()) return 'aborted';
    const after = d.getMountedArrayRange();
    // progressed = the mounted range moved toward the target since the step
    const progressed = plan.align === 'end' ? after.last > range.last : after.first < range.first;
    step = nextStepSize(step, progressed, window);
  }
  return 'exhausted';
}
