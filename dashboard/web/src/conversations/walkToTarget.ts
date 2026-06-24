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
