import { describe, it, expect } from 'vitest';
import {
  classifyWindowGrowth,
  applyOpenChange,
  resetPillTrackers,
  INITIAL_PILL_TRACKERS,
  createPagingGates,
  ARM_FALLBACK_MS,
  createOpenLifecycle,
  runJumpPipeline,
  resolveExhaustion,
  createFollowController,
  type PillTrackers,
  type PillGrowthInput,
  type GateTimerDeps,
  type JumpRunnerDeps,
  type JumpDrainResult,
} from './readerScrollMachine';

// #281 S5 A1 — the pill classifier decision table. These pin the ↓N-new
// live-append discriminator VERBATIM from the reader's `lastOp.rev`-keyed effect
// (ConversationReader.tsx): reset re-seeding, prepend/trim bail, the addedBottom
// tail slice, and the known-subagent / open-keys visibility classification.

type Item = { subagent_key: string | null };
const top = (): Item => ({ subagent_key: null });
const sub = (k: string): Item => ({ subagent_key: k });

function input(over: Partial<PillGrowthInput> & Pick<PillGrowthInput, 'op' | 'items'>): PillGrowthInput {
  return {
    hasMore: false,
    atBottom: false,
    openKeys: new Set<string>(),
    ...over,
  };
}

describe('classifyWindowGrowth', () => {
  it('(a) a reset seeds the known-set from the WHOLE window and never counts', () => {
    const items = [top(), sub('A'), sub('B'), top()];
    const r = classifyWindowGrowth(
      input({ op: { op: 'reset', addedBottom: 0 }, items, hasMore: true }),
      INITIAL_PILL_TRACKERS,
    );
    expect(r.visibleAdded).toBe(0);
    expect(r.countsTowardPill).toBe(false);
    expect([...r.nextTrackers.knownSubagentKeys].sort()).toEqual(['A', 'B']);
    expect(r.nextTrackers.prevLen).toBe(4);
    expect(r.nextTrackers.prevHasMore).toBe(true);
  });

  it('(b) a prepend NEVER counts even when prevHasMore === false && prevLen > 0', () => {
    const trackers: PillTrackers = { prevLen: 5, prevHasMore: false, knownSubagentKeys: new Set() };
    const items = [top(), top(), top(), top(), top(), top()];
    const r = classifyWindowGrowth(input({ op: { op: 'prepend', addedBottom: 0 }, items }), trackers);
    expect(r.visibleAdded).toBe(0);
    expect(r.countsTowardPill).toBe(false);
  });

  it('(c) a trim: true append with addedBottom: 0 never counts', () => {
    const trackers: PillTrackers = { prevLen: 5, prevHasMore: false, knownSubagentKeys: new Set() };
    const items = [top(), top(), top(), top(), top()];
    const r = classifyWindowGrowth(
      input({ op: { op: 'append', addedBottom: 0, trim: true }, items }),
      trackers,
    );
    expect(r.visibleAdded).toBe(0);
    expect(r.countsTowardPill).toBe(false);
  });

  it('(d) a live append of 2 top-level items → visibleAdded 2, countsTowardPill true', () => {
    const trackers: PillTrackers = { prevLen: 5, prevHasMore: false, knownSubagentKeys: new Set() };
    const items = [top(), top(), top(), top(), top(), top(), top()];
    const r = classifyWindowGrowth(input({ op: { op: 'append', addedBottom: 2 }, items }), trackers);
    expect(r.visibleAdded).toBe(2);
    expect(r.countsTowardPill).toBe(true);
  });

  it('(e) the first item of a brand-new subagent key counts ONCE even with 3 tail items in that key', () => {
    const trackers: PillTrackers = { prevLen: 5, prevHasMore: false, knownSubagentKeys: new Set() };
    const items = [top(), top(), top(), top(), top(), sub('A'), sub('A'), sub('A')];
    const r = classifyWindowGrowth(input({ op: { op: 'append', addedBottom: 3 }, items }), trackers);
    expect(r.visibleAdded).toBe(1);
    expect(r.countsTowardPill).toBe(true);
    expect(r.nextTrackers.knownSubagentKeys.has('A')).toBe(true);
  });

  it('(f) an append into a known + EXPANDED key counts each item', () => {
    const trackers: PillTrackers = { prevLen: 5, prevHasMore: false, knownSubagentKeys: new Set(['A']) };
    const items = [top(), top(), top(), top(), top(), sub('A'), sub('A')];
    const r = classifyWindowGrowth(
      input({ op: { op: 'append', addedBottom: 2 }, items, openKeys: new Set(['A']) }),
      trackers,
    );
    expect(r.visibleAdded).toBe(2);
  });

  it('(g) an append into a known + COLLAPSED key counts 0', () => {
    const trackers: PillTrackers = { prevLen: 5, prevHasMore: false, knownSubagentKeys: new Set(['A']) };
    const items = [top(), top(), top(), top(), top(), sub('A'), sub('A')];
    const r = classifyWindowGrowth(
      input({ op: { op: 'append', addedBottom: 2 }, items, openKeys: new Set() }),
      trackers,
    );
    expect(r.visibleAdded).toBe(0);
    expect(r.countsTowardPill).toBe(false);
  });

  it('(h) countsTowardPill is false when atBottom: true, though visibleAdded is unchanged', () => {
    const trackers: PillTrackers = { prevLen: 5, prevHasMore: false, knownSubagentKeys: new Set() };
    const items = [top(), top(), top(), top(), top(), top(), top()];
    const r = classifyWindowGrowth(
      input({ op: { op: 'append', addedBottom: 2 }, items, atBottom: true }),
      trackers,
    );
    expect(r.visibleAdded).toBe(2);
    expect(r.countsTowardPill).toBe(false);
  });

  it('(i) a non-live first page (prevLen: 0) seeds the known-set WITHOUT counting', () => {
    const items = [top(), sub('B'), sub('B')];
    const r = classifyWindowGrowth(
      input({ op: { op: 'append', addedBottom: 3 }, items }),
      INITIAL_PILL_TRACKERS,
    );
    expect(r.visibleAdded).toBe(0);
    expect(r.countsTowardPill).toBe(false);
    expect(r.nextTrackers.knownSubagentKeys.has('B')).toBe(true);
    expect(r.nextTrackers.prevLen).toBe(3);
  });

  it('(j) nextTrackers.prevHasMore equals the input hasMore on EVERY op kind', () => {
    const base: PillTrackers = { prevLen: 3, prevHasMore: false, knownSubagentKeys: new Set() };
    const items = [top(), top(), top()];
    for (const kind of [
      { op: { op: 'reset', addedBottom: 0 } as const, hasMore: true },
      { op: { op: 'prepend', addedBottom: 0 } as const, hasMore: false },
      { op: { op: 'append', addedBottom: 1 } as const, hasMore: true },
      { op: { op: 'append', addedBottom: 0, trim: true } as const, hasMore: false },
    ]) {
      const r = classifyWindowGrowth(input({ op: kind.op, items, hasMore: kind.hasMore }), base);
      expect(r.nextTrackers.prevHasMore).toBe(kind.hasMore);
      expect(r.nextTrackers.prevLen).toBe(items.length);
    }
  });

  it('(k) the input trackers object and its known-set are NOT mutated', () => {
    const known = new Set(['A']);
    const trackers: PillTrackers = Object.freeze({ prevLen: 5, prevHasMore: false, knownSubagentKeys: known });
    const items = [top(), top(), top(), top(), top(), sub('C'), sub('C')];
    const r = classifyWindowGrowth(input({ op: { op: 'append', addedBottom: 2 }, items }), trackers);
    // the input set never grew (its contents are still exactly {A})
    expect([...known]).toEqual(['A']);
    // the returned trackers are a fresh value carrying the new key
    expect(r.nextTrackers).not.toBe(trackers);
    expect(r.nextTrackers.knownSubagentKeys.has('C')).toBe(true);
    expect(trackers.prevLen).toBe(5);
  });

  it('op == null (initial render) never counts but advances the trackers', () => {
    const items: Item[] = [];
    const r = classifyWindowGrowth(input({ op: null, items, hasMore: true }), INITIAL_PILL_TRACKERS);
    expect(r.visibleAdded).toBe(0);
    expect(r.countsTowardPill).toBe(false);
    expect(r.nextTrackers.prevLen).toBe(0);
    expect(r.nextTrackers.prevHasMore).toBe(true);
  });
});

describe('applyOpenChange', () => {
  it('(l) add / remove round-trips immutably', () => {
    const empty: ReadonlySet<string> = new Set();
    const withX = applyOpenChange(empty, 'X', true);
    expect([...withX]).toEqual(['X']);
    expect(empty.size).toBe(0); // original untouched

    const withXY = applyOpenChange(withX, 'Y', true);
    expect([...withXY].sort()).toEqual(['X', 'Y']);
    expect([...withX]).toEqual(['X']); // original untouched

    const withoutX = applyOpenChange(withXY, 'X', false);
    expect([...withoutX]).toEqual(['Y']);
    expect([...withXY].sort()).toEqual(['X', 'Y']); // original untouched
  });
});

describe('resetPillTrackers', () => {
  it('(m) equals the INITIAL_PILL_TRACKERS shape (fresh empty set)', () => {
    const r = resetPillTrackers();
    expect(r.prevLen).toBe(0);
    expect(r.prevHasMore).toBe(false);
    expect(r.knownSubagentKeys.size).toBe(0);
    expect(INITIAL_PILL_TRACKERS.prevLen).toBe(0);
    expect(INITIAL_PILL_TRACKERS.prevHasMore).toBe(false);
    expect(INITIAL_PILL_TRACKERS.knownSubagentKeys.size).toBe(0);
    // a fresh call is a distinct value (no shared mutable set)
    expect(r.knownSubagentKeys).not.toBe(INITIAL_PILL_TRACKERS.knownSubagentKeys);
  });
});

// #281 S5 A2 — paging gates + programmatic-run tokens. Deps-injected fake timers
// (no vi.useFakeTimers needed) pin the arming / suppression / run-token contracts
// that today live as `reversePagingArmedRef`/`forwardPagingArmedRef`/
// `jumpDrainingRef`/`armPagingTimerRef`/`walkTokenRef` in ConversationReader.tsx.

function fakeTimers() {
  let nextId = 1;
  const pending = new Map<number, () => void>();
  const cleared: number[] = [];
  const deps: GateTimerDeps = {
    setTimeout(fn: () => void): number {
      const id = nextId++;
      pending.set(id, fn);
      return id;
    },
    clearTimeout(id: number): void {
      cleared.push(id);
      pending.delete(id);
    },
  };
  return {
    deps,
    cleared,
    pendingCount: () => pending.size,
    pendingIds: () => [...pending.keys()],
    fireAll() {
      for (const [id, fn] of [...pending]) { pending.delete(id); fn(); }
    },
  };
}

describe('createPagingGates', () => {
  it('(a) fresh gates never page (both edges disarmed)', () => {
    const t = fakeTimers();
    const g = createPagingGates(t.deps);
    expect(g.shouldPage('start')).toBe(false);
    expect(g.shouldPage('end')).toBe(false);
    // construction starts NO timer (StrictMode contract, spec §2)
    expect(t.pendingCount()).toBe(0);
  });

  it('(b) sessionOpened disarms; firing the fallback then arms both edges', () => {
    const t = fakeTimers();
    const g = createPagingGates(t.deps);
    g.arm(); // pretend a prior settle armed it
    g.sessionOpened();
    expect(g.shouldPage('start')).toBe(false);
    expect(g.shouldPage('end')).toBe(false);
    expect(t.pendingCount()).toBe(1); // the one-shot fallback is armed
    t.fireAll();
    expect(g.shouldPage('start')).toBe(true);
    expect(g.shouldPage('end')).toBe(true);
  });

  it('(c) arm() cancels the pending fallback with its armed id', () => {
    const t = fakeTimers();
    const g = createPagingGates(t.deps);
    g.sessionOpened();
    const [armedId] = t.pendingIds();
    g.arm();
    expect(t.cleared).toContain(armedId);
    expect(t.pendingCount()).toBe(0);
    expect(g.shouldPage('start')).toBe(true);
    expect(g.shouldPage('end')).toBe(true);
  });

  it('(d) a programmatic run suppresses BOTH edges even while armed', () => {
    const t = fakeTimers();
    const g = createPagingGates(t.deps);
    g.arm();
    expect(g.shouldPage('start')).toBe(true);
    g.beginProgrammaticRun();
    expect(g.shouldPage('start')).toBe(false);
    expect(g.shouldPage('end')).toBe(false);
  });

  it('(e) a stale endProgrammaticRun never releases a newer owner; the current one does', () => {
    const t = fakeTimers();
    const g = createPagingGates(t.deps);
    g.arm();
    const t1 = g.beginProgrammaticRun();
    const t2 = g.beginProgrammaticRun(); // supersede
    g.endProgrammaticRun(t1);            // stale — must NOT clear the newer run's suppression
    expect(g.shouldPage('start')).toBe(false);
    g.endProgrammaticRun(t2);            // current owner — releases
    expect(g.shouldPage('start')).toBe(true);
    expect(g.shouldPage('end')).toBe(true);
  });

  it('(f) isCurrentRun flips on supersession', () => {
    const t = fakeTimers();
    const g = createPagingGates(t.deps);
    const t1 = g.beginProgrammaticRun();
    expect(g.isCurrentRun(t1)).toBe(true);
    const t2 = g.beginProgrammaticRun();
    expect(g.isCurrentRun(t1)).toBe(false);
    expect(g.isCurrentRun(t2)).toBe(true);
  });

  it('(g) sessionOpened twice cancels the old fallback and arms a fresh one-shot', () => {
    const t = fakeTimers();
    const g = createPagingGates(t.deps);
    g.sessionOpened();
    const [firstId] = t.pendingIds();
    g.sessionOpened();
    expect(t.cleared).toContain(firstId);
    expect(t.pendingCount()).toBe(1); // exactly one live fallback
  });

  it('(h) dispose cancels the pending fallback', () => {
    const t = fakeTimers();
    const g = createPagingGates(t.deps);
    g.sessionOpened();
    expect(t.pendingCount()).toBe(1);
    g.dispose();
    expect(t.pendingCount()).toBe(0);
  });

  it('ARM_FALLBACK_MS is 750 and passed to the injected setTimeout', () => {
    let capturedMs = -1;
    const deps: GateTimerDeps = {
      setTimeout(_fn: () => void, ms: number): number { capturedMs = ms; return 1; },
      clearTimeout(): void { /* noop */ },
    };
    const g = createPagingGates(deps);
    g.sessionOpened();
    expect(capturedMs).toBe(ARM_FALLBACK_MS);
    expect(ARM_FALLBACK_MS).toBe(750);
  });
});

// #281 S5 A3 — open-lifecycle latches. Three DISTINCT generation-keyed events
// (spec F1): SESSION_CHANGED (reset defaults), FIRST_WINDOW_READY (the one-shot
// landing), RESTORE_READY (the restore jump), replacing `appliedIntentRef` /
// `restoredRef` + `lastOpenSessionRef`. Latches are keyed on the OPEN GENERATION
// (not the sessionId value) so an A→B→A return re-arms.

describe('createOpenLifecycle', () => {
  it('(a) sessionChanged is idempotent per generation and re-fires on a new one', () => {
    const lc = createOpenLifecycle();
    expect(lc.sessionChanged(1, 'tail')).toEqual({ atBottom: true });
    expect(lc.sessionChanged(1, 'tail')).toBeNull(); // idempotent
    expect(lc.sessionChanged(2, 'tail')).toEqual({ atBottom: true }); // re-arm
  });

  it('(b) atBottom default: anchor/restore → false, tail/null → true', () => {
    const lc = createOpenLifecycle();
    expect(lc.sessionChanged(1, 'anchor')).toEqual({ atBottom: false });
    expect(lc.sessionChanged(2, 'restore')).toEqual({ atBottom: false });
    expect(lc.sessionChanged(3, 'tail')).toEqual({ atBottom: true });
    expect(lc.sessionChanged(4, null)).toEqual({ atBottom: true });
  });

  it('(c) firstWindowReady lands exactly once per generation and re-fires on a new one', () => {
    const lc = createOpenLifecycle();
    // null intent → no landing
    expect(lc.firstWindowReady(1, null, 5, 5)).toBeNull();
    // zero counts → wait
    expect(lc.firstWindowReady(1, 'top', 0, 5)).toBeNull();
    expect(lc.firstWindowReady(1, 'top', 5, 0)).toBeNull();
    // resolved → lands once
    expect(lc.firstWindowReady(1, 'top', 5, 5)).toEqual({ target: 'top', setAtBottom: false });
    // later calls this generation → null
    expect(lc.firstWindowReady(1, 'top', 5, 5)).toBeNull();
    expect(lc.firstWindowReady(1, 'bottom', 5, 5)).toBeNull();
    // a NEW generation re-fires
    expect(lc.firstWindowReady(2, 'bottom', 9, 9)).toEqual({ target: 'bottom', setAtBottom: true });
  });

  it('(d) restoreReady fires once for a matching restore open, and re-arms A→B→A', () => {
    const lc = createOpenLifecycle();
    // non-restore intent → never
    expect(lc.restoreReady(1, 'tail', null, 'A', 'A')).toBeNull();
    // restore intent but detail mismatched → wait
    expect(lc.restoreReady(1, 'restore', 'u-a', 'OTHER', 'A')).toBeNull();
    // restore intent, detail matches → fires once
    expect(lc.restoreReady(1, 'restore', 'u-a', 'A', 'A')).toEqual({ uuid: 'u-a' });
    // later this generation → null
    expect(lc.restoreReady(1, 'restore', 'u-a', 'A', 'A')).toBeNull();
    // gen 2 = B as a NON-restore tail open → never fires
    expect(lc.restoreReady(2, 'tail', null, 'B', 'B')).toBeNull();
    // gen 3 = return to A as a restore open → fires AGAIN (re-armed by generation)
    expect(lc.restoreReady(3, 'restore', 'u-a', 'A', 'A')).toEqual({ uuid: 'u-a' });
  });

  it('(d2) restoreReady is null for a null restore uuid or a null detailSessionId', () => {
    const lc = createOpenLifecycle();
    expect(lc.restoreReady(1, 'restore', null, 'A', 'A')).toBeNull();
    expect(lc.restoreReady(1, 'restore', 'u-a', null, 'A')).toBeNull();
  });

  it('(e) StrictMode rehearsal: doubled sessionChanged + doubled firstWindowReady → one landing total', () => {
    const lc = createOpenLifecycle();
    // setup → cleanup → setup rehearsal calls the same generation twice
    lc.sessionChanged(1, 'tail');
    lc.sessionChanged(1, 'tail'); // no-op
    const first = lc.firstWindowReady(1, 'bottom', 4, 4);
    const second = lc.firstWindowReady(1, 'bottom', 4, 4);
    const landings = [first, second].filter((c) => c != null);
    expect(landings).toHaveLength(1);
    expect(landings[0]).toEqual({ target: 'bottom', setAtBottom: true });
  });
});

// #281 S5 A4 — the jump-pipeline runner. Fake deps + a call log pin the CONTROL
// FLOW verbatim (spec §3-A4): drain → captured mode-check → captured resolveHit
// → hit: ownerChain? defer : (mounted? skip-walk : walk) → quiesce → land branch
// by hasCardRef / expandDetails+findOpen / default → landedBookkeeping; no-hit:
// fallbackChain? defer : (resolveExhaustion(committedRev, drain) === 'clear'?
// clearJump : deferred). Every await is followed by an abort check. The #286 race
// is FIXED by the committed-window-epoch gate (#286 B3; tests f/g/g3/g4).

function makeJumpDeps(over: {
  abortAfter?: string;                         // name of the dep after which aborted() flips true
  modeHidden?: 'reset-needed' | 'proceed';
  resolveHit?: { arrayIndex: number } | null;
  ownerChain?: string[] | null;
  fallbackChain?: string[] | null;
  isTargetMounted?: boolean;
  walk?: 'mounted' | 'exhausted';
  hasLandableElement?: boolean;
  hasCardRef?: boolean;
  findOpen?: boolean;
  expandDetails?: boolean;
  drain?: Partial<JumpDrainResult>;   // #286 B3 — the loadToTarget committed-epoch result
  committedRev?: number;              // #286 B3 — the captured lastOp.rev at effect-fire
} = {}): { deps: JumpRunnerDeps; log: string[] } {
  const log: string[] = [];
  let aborted = false;
  const mark = (name: string) => {
    log.push(name);
    if (over.abortAfter === name) aborted = true;
  };
  const deps: JumpRunnerDeps = {
    aborted: () => aborted,
    loadToTarget: async () => {
      mark('loadToTarget');
      return { found: false, exhausted: true, terminalOpRev: 0, ...over.drain };
    },
    committedRev: over.committedRev ?? 0,
    captured: {
      modeHidden: () => { mark('modeHidden'); return over.modeHidden ?? 'proceed'; },
      resolveHit: () => { mark('resolveHit'); return over.resolveHit === undefined ? { arrayIndex: 3 } : over.resolveHit; },
      ownerChainToOpen: () => { mark('ownerChainToOpen'); return over.ownerChain ?? null; },
      fallbackChainToOpen: () => { mark('fallbackChainToOpen'); return over.fallbackChain ?? null; },
    },
    live: {
      isTargetMounted: () => { mark('isTargetMounted'); return over.isTargetMounted ?? true; },
      walk: async () => { mark('walk'); return over.walk ?? 'mounted'; },
      quiesce: async () => { mark('quiesce'); },
      openDisclosures: () => { mark('openDisclosures'); },
      landCard: async () => { mark('landCard'); },
      landFindReassert: async () => { mark('landFindReassert'); },
      landCenter: async () => { mark('landCenter'); },
      hasCardRef: () => { mark('hasCardRef'); return over.hasCardRef ?? false; },
      hasLandableElement: () => { mark('hasLandableElement'); return over.hasLandableElement ?? true; },
      findOpen: () => { mark('findOpen'); return over.findOpen ?? false; },
      requestForceOpen: (chain: string[]) => { mark(`requestForceOpen:${chain.join(',')}`); },
      dispatchModeReset: () => { mark('dispatchModeReset'); },
      landedBookkeeping: (arrayIndex: number) => { mark(`landedBookkeeping:${arrayIndex}`); },
      clearJump: () => { mark('clearJump'); },
    },
    expandDetails: over.expandDetails ?? false,
  };
  return { deps, log };
}

describe('runJumpPipeline', () => {
  it('(a) a happy warm jump runs drain → modeHidden → resolveHit → mounted → quiesce → findOpen → landCenter → bookkeeping', async () => {
    const { deps, log } = makeJumpDeps({ isTargetMounted: true });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('landed');
    // #291 — the landing branch is now `live.findOpen()` (was `deps.expandDetails &&
    // live.findOpen()`), so a find-closed warm jump consults findOpen() before falling
    // through to the cheap single-shot landCenter (a pure ref read, no side effect).
    expect(log).toEqual([
      'loadToTarget', 'modeHidden', 'resolveHit', 'ownerChainToOpen',
      'isTargetMounted', 'hasLandableElement', 'quiesce', 'hasCardRef',
      'findOpen', 'landCenter', 'landedBookkeeping:3',
    ]);
  });

  it('(b) abort after loadToTarget stops the pipeline with no further deps', async () => {
    const { deps, log } = makeJumpDeps({ abortAfter: 'loadToTarget' });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('aborted');
    expect(log).toEqual(['loadToTarget']);
  });

  it('(c) a mode-hidden target resets the mode once and defers; a proceed run never resets', async () => {
    const hidden = makeJumpDeps({ modeHidden: 'reset-needed' });
    expect(await runJumpPipeline(hidden.deps)).toBe('deferred');
    expect(hidden.log).toEqual(['loadToTarget', 'modeHidden', 'dispatchModeReset']);

    const proceed = makeJumpDeps({ modeHidden: 'proceed', isTargetMounted: true });
    await runJumpPipeline(proceed.deps);
    expect(proceed.log).not.toContain('dispatchModeReset');
  });

  it('(d) an owner-chain hit requests the force-open chain and defers, with no scroll', async () => {
    const { deps, log } = makeJumpDeps({ ownerChain: ['g', 'p'] });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('deferred');
    expect(log).toEqual(['loadToTarget', 'modeHidden', 'resolveHit', 'ownerChainToOpen', 'requestForceOpen:g,p']);
    expect(log).not.toContain('quiesce');
    expect(log).not.toContain('landCenter');
  });

  it('(e) a cold jump walks; a walk that exhausts skips the landing branch but still books the flash', async () => {
    const { deps, log } = makeJumpDeps({ isTargetMounted: false, walk: 'exhausted' });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('landed'); // exhausted falls through to arm+flash+pin+cursor (bug-for-bug)
    expect(log).toEqual([
      'loadToTarget', 'modeHidden', 'resolveHit', 'ownerChainToOpen',
      'isTargetMounted', 'walk', 'landedBookkeeping:3',
    ]);
    expect(log).not.toContain('quiesce');
    expect(log).not.toContain('landCenter');
  });

  // #286 B3 — the Task-4 (g) "clear on captured !hasMore" case is UPDATED to the
  // committed-window-epoch gate (deferral-broaden): the first no-hit whose committed
  // epoch still LAGS the drain's terminal op DEFERS (pendingExhaustion), and only a
  // caught-up epoch with the target genuinely absent + the drained edge exhausted
  // clears. The #286 race is now FIXED, not preserved.
  it('(f) no-hit while the committed epoch lags the drain terminal op defers (pendingExhaustion), never clears', async () => {
    const { deps, log } = makeJumpDeps({
      resolveHit: null, drain: { found: false, exhausted: true, terminalOpRev: 7 }, committedRev: 5,
    });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('deferred');
    expect(log).toEqual(['loadToTarget', 'modeHidden', 'resolveHit', 'fallbackChainToOpen']);
    expect(log).not.toContain('clearJump');
  });

  it('(g) no-hit with the committed epoch caught up + drained edge exhausted clears once (#286 B3)', async () => {
    const { deps, log } = makeJumpDeps({
      resolveHit: null, drain: { found: false, exhausted: true, terminalOpRev: 4 }, committedRev: 4,
    });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('exhausted-cleared');
    expect(log).toEqual(['loadToTarget', 'modeHidden', 'resolveHit', 'fallbackChainToOpen', 'clearJump']);
  });

  it('(g2) no-hit with a fallback chain requests it and defers (no clear)', async () => {
    const { deps, log } = makeJumpDeps({
      resolveHit: null, fallbackChain: ['k'], drain: { found: false, exhausted: true, terminalOpRev: 0 }, committedRev: 9,
    });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('deferred');
    expect(log).toEqual(['loadToTarget', 'modeHidden', 'resolveHit', 'fallbackChainToOpen', 'requestForceOpen:k']);
    expect(log).not.toContain('clearJump');
  });

  it('(g3) no-hit but the drain reached the target in the LIVE window (the #286 race) defers, never clears', async () => {
    // The captured render list is a stale pre-commit snapshot; the target is in the
    // live window and its commit will surface it → defer, even with the epoch ahead.
    const { deps, log } = makeJumpDeps({
      resolveHit: null, drain: { found: true, exhausted: true, terminalOpRev: 2 }, committedRev: 9,
    });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('deferred');
    expect(log).not.toContain('clearJump');
  });

  it('(g4) no-hit with the drained edge NOT exhausted (fetch failure / more to page) defers, never clears', async () => {
    const { deps, log } = makeJumpDeps({
      resolveHit: null, drain: { found: false, exhausted: false, terminalOpRev: 1 }, committedRev: 9,
    });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('deferred');
    expect(log).not.toContain('clearJump');
  });

  it('(h) expandDetails + no card opens disclosures before quiesce and picks landFindReassert', async () => {
    const { deps, log } = makeJumpDeps({ isTargetMounted: true, expandDetails: true, findOpen: true, hasCardRef: false });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('landed');
    expect(log).toEqual([
      'loadToTarget', 'modeHidden', 'resolveHit', 'ownerChainToOpen',
      'isTargetMounted', 'hasLandableElement', 'openDisclosures', 'quiesce',
      'hasCardRef', 'findOpen', 'landFindReassert', 'landedBookkeeping:3',
    ]);
    // openDisclosures precedes quiesce
    expect(log.indexOf('openDisclosures')).toBeLessThan(log.indexOf('quiesce'));
  });

  it('(i) a card-root hit picks landCard over center', async () => {
    const { deps, log } = makeJumpDeps({ isTargetMounted: true, hasCardRef: true });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('landed');
    expect(log).toContain('landCard');
    expect(log).not.toContain('landCenter');
    expect(log).not.toContain('landFindReassert');
  });

  it('(j) abort after quiesce skips the landing + bookkeeping', async () => {
    const { deps, log } = makeJumpDeps({ isTargetMounted: true, abortAfter: 'quiesce' });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('aborted');
    expect(log).toEqual([
      'loadToTarget', 'modeHidden', 'resolveHit', 'ownerChainToOpen',
      'isTargetMounted', 'hasLandableElement', 'quiesce',
    ]);
  });

  // #291 Part 2 — EVERY find landing must route through the convergent every-frame
  // reassert so the center survives virtuoso's deferred ResizeObserver re-measure
  // (a plain-prose find hit whose `expand_details` is false used to fall through to
  // the single-shot `landCenter`, which the deferred re-measure then clobbered — the
  // top-reset). The branch is relaxed from `deps.expandDetails && live.findOpen()`
  // to just `live.findOpen()`; `openDisclosures()` stays expand-gated. RED lever:
  // with the OLD condition, case (k) records `landCenter` (not `landFindReassert`).
  it('(k) #291 — a plain-prose find landing (findOpen, expandDetails=false) routes through landFindReassert, NOT landCenter/openDisclosures', async () => {
    const { deps, log } = makeJumpDeps({
      isTargetMounted: true, hasLandableElement: true, hasCardRef: false,
      findOpen: true, expandDetails: false,
    });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('landed');
    expect(log).toContain('landFindReassert');
    expect(log).not.toContain('landCenter');
    expect(log).not.toContain('openDisclosures');
  });

  it('(l) #291 — a tool/thinking find landing (expandDetails + findOpen) opens disclosures BEFORE quiesce and still routes through landFindReassert', async () => {
    const { deps, log } = makeJumpDeps({
      isTargetMounted: true, hasLandableElement: true, hasCardRef: false,
      findOpen: true, expandDetails: true,
    });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('landed');
    expect(log).toContain('openDisclosures');
    expect(log).toContain('landFindReassert');
    expect(log).not.toContain('landCenter');
    expect(log.indexOf('openDisclosures')).toBeLessThan(log.indexOf('quiesce'));
  });

  it('(m) #291 — a find-CLOSED jump (no open find bar) keeps the cheap single-shot landCenter', async () => {
    const { deps, log } = makeJumpDeps({
      isTargetMounted: true, hasLandableElement: true, hasCardRef: false,
      findOpen: false, expandDetails: false,
    });
    const outcome = await runJumpPipeline(deps);
    expect(outcome).toBe('landed');
    expect(log).toContain('landCenter');
    expect(log).not.toContain('landFindReassert');
    expect(log).not.toContain('openDisclosures');
  });
});

// #281 S5 B3 — the committed-window-epoch exhaustion gate (#286). `resolveExhaustion`
// is the pure decision the runner's no-hit clear consults: it fires ONLY when the
// target is genuinely absent (it did NOT reach the live window, the DRAINED edge is
// truly exhausted — directional: hasPrev for a backward drain — AND the captured
// epoch has caught up to the drain's terminal op). Every other case defers
// (pendingExhaustion), so a stale pre-commit captured render list can never fire a
// premature clear (the #286 race).

describe('resolveExhaustion', () => {
  const drain = (over: Partial<JumpDrainResult> = {}): JumpDrainResult =>
    ({ found: false, exhausted: true, terminalOpRev: 0, ...over });

  it('(a) a first no-hit whose committed epoch LAGS the terminal op → defer (pendingExhaustion)', () => {
    // A bringing-prepend is still in flight to commit; wait for its re-fire.
    expect(resolveExhaustion(3, drain({ terminalOpRev: 5 }))).toBe('defer');
  });

  it('(b) committed epoch caught up + still-absent + drained edge exhausted → clear', () => {
    expect(resolveExhaustion(5, drain({ terminalOpRev: 5 }))).toBe('clear');
    expect(resolveExhaustion(6, drain({ terminalOpRev: 5 }))).toBe('clear');
  });

  it('(c) the target reached the live window (found) → defer, never clear (the #286 race)', () => {
    // Even with the epoch ahead: the target IS in the live window, just not yet in
    // the (stale captured) render list — its commit will surface it.
    expect(resolveExhaustion(9, drain({ found: true, terminalOpRev: 1 }))).toBe('defer');
  });

  it('(d) the DRAINED edge is not exhausted (directional: hasPrev still true) → defer', () => {
    // A backward drain keys on hasPrev, NOT the bottom edge (spec F8): the adapter
    // reports exhausted=false while hasPrev is true, so the clear can never fire on
    // the wrong (bottom) edge.
    expect(resolveExhaustion(9, drain({ exhausted: false, terminalOpRev: 1 }))).toBe('defer');
  });

  it('(e) a fetch-failure / session-change exit leaves exhausted false → defer (stays pending)', () => {
    expect(resolveExhaustion(100, drain({ exhausted: false, terminalOpRev: 0 }))).toBe('defer');
  });
});

// #281 S5 B1 — the follow-suspension controller (#285 open-position). The reader
// passes literal `followOutput={false}` while suspended so react-virtuoso's
// raw-truthy resize-autoscroll-to-LAST watcher is DISABLED and a 'top' landing
// (single-page open) actually sticks. Suspension is ON for a 'top' landing (until
// settle) and for an anchor/restore open (until the jump-landing settle); a
// multi-page 'bottom' tail open stays live from the start so live-tail sticks.

describe('createFollowController', () => {
  it('(a) a top landing suspends follow until settle; a tail open is live from the start', () => {
    const f = createFollowController();
    expect(f.followMode()).toBe('live');
    f.openChanged('tail');
    expect(f.followMode()).toBe('live');   // a tail open keeps stick live until we learn top-vs-bottom
    f.landed('top');
    expect(f.followMode()).toBe('suspended');
    f.settle();
    expect(f.followMode()).toBe('live');
  });

  it('(b) a bottom landing stays live throughout (multi-page tail stick intact)', () => {
    const f = createFollowController();
    f.openChanged('tail');
    f.landed('bottom');
    expect(f.followMode()).toBe('live');
    f.settle();
    expect(f.followMode()).toBe('live');
  });

  it('(c) an anchor / restore open is suspended until the (jump-landing) settle', () => {
    const anchor = createFollowController();
    anchor.openChanged('anchor');
    expect(anchor.followMode()).toBe('suspended');
    anchor.settle();                       // landedBookkeeping-driven
    expect(anchor.followMode()).toBe('live');

    const restore = createFollowController();
    restore.openChanged('restore');
    expect(restore.followMode()).toBe('suspended');
  });

  it('(d) a legacy (null) open is live', () => {
    const f = createFollowController();
    f.openChanged(null);
    expect(f.followMode()).toBe('live');
  });

  it('(e) a fresh open RESETS a prior suspension (openChanged is the per-open reset)', () => {
    const f = createFollowController();
    f.openChanged('anchor');
    expect(f.followMode()).toBe('suspended');
    f.openChanged('tail');                 // the next open
    expect(f.followMode()).toBe('live');
    // a top landing in the reused controller suspends again
    f.landed('top');
    expect(f.followMode()).toBe('suspended');
  });
});
