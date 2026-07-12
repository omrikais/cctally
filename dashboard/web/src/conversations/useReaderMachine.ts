import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  createPagingGates, createOpenLifecycle, createFollowController,
  type PagingGates, type OpenLifecycle, type FollowController, type FollowMode,
} from './readerScrollMachine';

// #281 S5 — the reader's scroll-machine ADAPTER. Owns the ONE machine instance
// per reader and supplies its real side-effect deps (timers now; DOM/store
// closures in Task 4). The pure `readerScrollMachine` module has NO React / DOM /
// rAF import — everything is injected here.
//
// StrictMode lifecycle contract (spec §2): every instance is constructed lazily
// via a ref (so React's double-render can't build two), construction starts NO
// timers (the machine only arms them from `sessionOpened`, called by the reader's
// session-switch effect), and the unmount cleanup disposes pending timers without
// erasing any consumed run-token / latch state.
/** The reader-facing follow-suspension surface (#281 S5 B1). The controller is
 *  pure; these thin wrappers mirror its `followMode()` into `followMode` React
 *  state AFTER each transition, so the reader can flip `followOutput` between the
 *  live callback and literal `false`. Identity-stable (memoized) so threading it
 *  never churns a memoized context. */
export interface FollowSurface {
  openChanged(intentKind: 'anchor' | 'restore' | 'tail' | null): void;
  landed(target: 'top' | 'bottom'): void;
  settle(): void;
}

export interface ReaderMachine {
  gates: PagingGates;
  lifecycle: OpenLifecycle;
  /** The OPEN GENERATION — a monotonic counter bumped whenever `sessionId`
   *  changes (the open-instance key). The lifecycle latches are keyed on it, so
   *  an A→B→A return re-arms even though the reader is mounted persistently. */
  generation: number;
  /** The live follow mode (#281 S5 B1). `'suspended'` → the reader passes literal
   *  `followOutput={false}` (disabling react-virtuoso's raw-truthy
   *  resize-autoscroll-to-LAST watcher); `'live'` → the stick callback. Changes
   *  ONLY on a real machine transition, so it never churns per render. */
  followMode: FollowMode;
  /** The follow-suspension transitions (identity-stable). */
  follow: FollowSurface;
}

export function useReaderMachine(sessionId: string): ReaderMachine {
  const gatesRef = useRef<PagingGates | null>(null);
  if (gatesRef.current == null) {
    gatesRef.current = createPagingGates({
      setTimeout: (fn, ms) => window.setTimeout(fn, ms),
      clearTimeout: (id) => window.clearTimeout(id),
    });
  }
  const gates = gatesRef.current;

  const lifecycleRef = useRef<OpenLifecycle | null>(null);
  if (lifecycleRef.current == null) {
    lifecycleRef.current = createOpenLifecycle();
  }
  const lifecycle = lifecycleRef.current;

  // #281 S5 B1 — the follow-suspension controller (pure) + its React-state mirror.
  // The controller holds the truth; `followMode` state is set ONLY when a
  // transition actually changes the mode (the `prev === next ? prev : next`
  // guard), so threading `followMode`/`follow` never churns a memoized context.
  const followRef = useRef<FollowController | null>(null);
  if (followRef.current == null) {
    followRef.current = createFollowController();
  }
  const followController = followRef.current;
  const [followMode, setFollowMode] = useState<FollowMode>('live');
  const syncFollow = useCallback(() => {
    const next = followController.followMode();
    setFollowMode((prev) => (prev === next ? prev : next));
  }, [followController]);
  const follow = useMemo<FollowSurface>(() => ({
    openChanged: (intentKind) => { followController.openChanged(intentKind); syncFollow(); },
    landed: (target) => { followController.landed(target); syncFollow(); },
    settle: () => { followController.settle(); syncFollow(); },
  }), [followController, syncFollow]);

  // The open-generation counter. Bumped during render when `sessionId` changes,
  // guarded by `genSessionRef` so React's StrictMode double-render (and any
  // re-render that keeps the same sessionId) increments it AT MOST ONCE per real
  // session change — a deterministic, idempotent render-phase derivation (the
  // documented "adjust a ref when a prop changes" pattern). The bump does NOT
  // fire the lander prematurely on the bare session-change render: the lander
  // effect's deps (openScrollIntent / items.length / nodes.length) are unchanged
  // on that render (the hook still holds the prior session's state), so it doesn't
  // run until the hook resets those to 0 and then resolves the new page — by which
  // point the generation is already the new one and the data is consistent.
  const generationRef = useRef(0);
  const genSessionRef = useRef<string | null>(null);
  if (genSessionRef.current !== sessionId) {
    genSessionRef.current = sessionId;
    generationRef.current += 1;
  }
  const generation = generationRef.current;

  // Cancel the fallback timer on unmount (was the reader's dedicated
  // arm-fallback-timer cleanup effect). dispose() only cancels the timer — it
  // never resets arming / run-token / latch state, so a StrictMode
  // setup→cleanup→setup rehearsal neither duplicates nor loses work.
  useEffect(() => () => gates.dispose(), [gates]);
  return { gates, lifecycle, generation, followMode, follow };
}
