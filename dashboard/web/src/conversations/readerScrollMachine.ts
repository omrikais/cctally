// #281 S5 — the reader's scroll / pager / live-tail ORCHESTRATION machine, pure
// and DOM-free (every side effect is injected by the `useReaderMachine` adapter,
// mirroring the walkToTarget / reassertCenter precedent). This module owns the
// ordering CONTRACTS between the already-extracted pure helpers; the adapter
// supplies the React/Virtuoso/store wiring. No React import, no DOM globals, no
// direct rAF / Date.now — see the spec §2 StrictMode lifecycle contract and the
// §3 snapshot-vs-live table (both BINDING).

import type { WindowOp } from '../hooks/useConversation';

// ---------------------------------------------------------------------------
// A1 — Pill classifier (the "↓ N new" live-append discriminator).
//
// Moved VERBATIM from the reader's `lastOp.rev`-keyed effect: a reset re-seeds
// the known-subagent set from the whole window; a prepend / trim never counts;
// a real append classifies the last `addedBottom` items against the OLD
// known/open sets. The trackers are IMMUTABLE VALUES (spec F5) — the classifier
// never mutates its input and always returns a fresh `nextTrackers`.
// ---------------------------------------------------------------------------

/** Immutable per-open pill state. `prevHasMore` is recorded on EVERY op (spec
 *  F5); `prevLen` gates the live discriminator; `knownSubagentKeys` records
 *  every subagent_key seen on a prior commit. */
export interface PillTrackers {
  readonly prevLen: number;
  readonly prevHasMore: boolean;
  readonly knownSubagentKeys: ReadonlySet<string>;
}

/** The per-open seed (before any window op) — prevLen 0, prevHasMore false,
 *  empty known-set. Never mutated; `resetPillTrackers()` mints fresh copies. */
export const INITIAL_PILL_TRACKERS: PillTrackers = {
  prevLen: 0,
  prevHasMore: false,
  knownSubagentKeys: new Set<string>(),
};

/** Session-switch reset — a FRESH tracker value (a distinct empty set, so no two
 *  readers share a mutable set). */
export function resetPillTrackers(): PillTrackers {
  return { prevLen: 0, prevHasMore: false, knownSubagentKeys: new Set<string>() };
}

export interface PillGrowthInput {
  /** The window op that triggered this commit (`lastOp`), or null on the initial
   *  render. Only `op` / `addedBottom` / `trim` are read. */
  op: Pick<WindowOp, 'op' | 'addedBottom' | 'trim'> | null;
  /** The current window items (only `subagent_key` is read). */
  items: ReadonlyArray<{ subagent_key: string | null }>;
  /** Recorded into `nextTrackers.prevHasMore` on EVERY op kind (spec F5). */
  hasMore: boolean;
  /** Gates `countsTowardPill` — at bottom, followOutput sticks and the count is
   *  reset by `atBottomStateChange`, so a bump here would double-count. */
  atBottom: boolean;
  /** The adapter-owned set of currently-EXPANDED subagent keys (fed by
   *  `applyOpenChange`). */
  openKeys: ReadonlySet<string>;
}

export interface PillGrowthResult {
  /** The count of newly-VISIBLE live-appended items (0 unless a live append). */
  visibleAdded: number;
  /** `live && visibleAdded > 0 && !atBottom` — whether to bump the pill. */
  countsTowardPill: boolean;
  /** A fresh immutable tracker value; the input is never mutated. */
  nextTrackers: PillTrackers;
}

/** Seed `known` with every non-null subagent_key in `source`, returning a NEW
 *  set only when at least one key is added (else the same reference — safe
 *  because the set is treated as immutable). */
function seedKnown(
  known: ReadonlySet<string>,
  source: ReadonlyArray<{ subagent_key: string | null }>,
): ReadonlySet<string> {
  let next: Set<string> | null = null;
  for (const it of source) {
    if (it.subagent_key != null && !known.has(it.subagent_key)) {
      if (next == null) next = new Set(known);
      next.add(it.subagent_key);
    }
  }
  return next ?? known;
}

/** Classify a window growth into a pill count + the next tracker value. Pure —
 *  reads the OLD `trackers`/`openKeys`, never mutates them. */
export function classifyWindowGrowth(input: PillGrowthInput, trackers: PillTrackers): PillGrowthResult {
  const { op, items, hasMore, atBottom, openKeys } = input;
  const len = items.length;
  const known = trackers.knownSubagentKeys;

  // A reset replaces the whole window, so it SEEDS the known-subagent set from
  // the ENTIRE new window (mirrors the old code). A prepend replaces nothing +
  // never counts. Both short-circuit (advancing the prev-trackers) so this path
  // never miscounts a prepend as a live append.
  if (op != null && op.op !== 'append') {
    const nextKnown = op.op === 'reset' ? seedKnown(known, items) : known;
    return {
      visibleAdded: 0,
      countsTowardPill: false,
      nextTrackers: { prevLen: len, prevHasMore: hasMore, knownSubagentKeys: nextKnown },
    };
  }

  // A real append takes `added = op.addedBottom` (the trim: true append also
  // lands here with addedBottom 0 → no count). `op == null` (initial render)
  // takes added 0 too.
  const added = op?.op === 'append' ? op.addedBottom : 0;
  // Live append (not the final pagination page): already fully paged before this
  // growth (prevHasMore === false), and not the very first page load (prevLen > 0).
  const live = added > 0 && trackers.prevHasMore === false && trackers.prevLen > 0;
  // The newly-appended items are the LAST `added` items of the window — trim-safe
  // because any top-drop shifts indices but never the bottom slice's content.
  const tail = added > 0 ? items.slice(len - added) : [];
  let visibleAdded = 0;
  if (live) {
    // Classify each newly-appended item by VISIBILITY against the OLD known-set +
    // open-set: top-level (+1); first item of a brand-new subagent group (+1,
    // deduped per key per tick); append into an already-EXPANDED known thread
    // (+1); append into an existing COLLAPSED known thread (+0, below the fold).
    const newThisTick = new Set<string>();
    for (const it of tail) {
      const k = it.subagent_key;
      if (k == null) {
        visibleAdded++;                       // top-level → always visible
      } else if (!known.has(k)) {
        if (!newThisTick.has(k)) { visibleAdded++; newThisTick.add(k); }
      } else if (openKeys.has(k)) {
        visibleAdded++;                       // append into an expanded thread
      }
      // else: append into an existing collapsed thread → +0 (below the fold).
    }
  }
  // Update the known-set from the tail AFTER the visibility classification read
  // the OLD set — covers both live and non-live (seed) growth.
  const nextKnown = seedKnown(known, tail);
  return {
    visibleAdded,
    countsTowardPill: live && visibleAdded > 0 && !atBottom,
    nextTrackers: { prevLen: len, prevHasMore: hasMore, knownSubagentKeys: nextKnown },
  };
}

/** Immutable open/collapse of a subagent thread (today's `handleSubagentOpenChange`,
 *  spec F5). Returns a fresh set — never mutates the input. */
export function applyOpenChange(openKeys: ReadonlySet<string>, key: string, open: boolean): ReadonlySet<string> {
  const next = new Set(openKeys);
  if (open) next.add(key);
  else next.delete(key);
  return next;
}

// ---------------------------------------------------------------------------
// A2 — Paging gates + programmatic-run tokens.
//
// The arming / suppression cluster (`reversePagingArmed`/`forwardPagingArmed`,
// `jumpDraining`, the 750ms session-open fallback timer, `armPaging()`
// idempotence) + the walk-token ownership rule (`walkTokenRef`), as one pure
// slice with injected timers. `startReached`/`endReached` consult
// `shouldPage(edge)`; a programmatic jump run owns a monotonic token via
// `beginProgrammaticRun()` / `endProgrammaticRun(token)` — the latter releases
// suppression ONLY when the token is still the current owner (a superseded run
// never clears the newer owner's suppression, spec F4).
// ---------------------------------------------------------------------------

/** The one-shot session-open fallback delay: arms paging even if neither settle
 *  signal (first atBottomStateChange / a jump landing) ever fires. */
export const ARM_FALLBACK_MS = 750;

/** Injected timer surface (real `window.setTimeout`/`clearTimeout` in the
 *  adapter; a deterministic fake in the vitest). Numeric ids match the DOM. */
export interface GateTimerDeps {
  setTimeout(fn: () => void, ms: number): number;
  clearTimeout(id: number): void;
}

export interface PagingGates {
  /** A session open: disarm both edges + drop any in-flight suppression, then
   *  cancel-and-rearm the one-shot fallback timer. */
  sessionOpened(): void;
  /** Arm both edges (the open settled) + cancel the fallback. Idempotent. */
  arm(): void;
  /** `armed[edge] && no programmatic run in flight` — the startReached /
   *  endReached predicate. */
  shouldPage(edge: 'start' | 'end'): boolean;
  /** Bump the run token + suppress both edges; returns THIS run's token. */
  beginProgrammaticRun(): number;
  /** Release suppression ONLY if `token` is still the current owner (spec F4). */
  endProgrammaticRun(token: number): void;
  /** `token === current owner` — the walk-token half of the runner's aborted(). */
  isCurrentRun(token: number): boolean;
  /** Cancel the fallback timer (unmount). */
  dispose(): void;
}

// ---------------------------------------------------------------------------
// A3 — Open lifecycle (three DISTINCT generation-keyed events, spec F1).
//
// `SESSION_CHANGED` fires on a session switch and returns the reset defaults
// (atBottom by intent kind); `FIRST_WINDOW_READY` fires when the intent is
// resolved AND items AND rendered nodes are non-empty and returns the one-shot
// landing command; `RESTORE_READY` fires when a restore-intent open has matching
// detail and returns the restore-jump uuid. Every latch is keyed on the OPEN
// GENERATION (not the sessionId value), so an A→B→A return re-arms — replacing
// `appliedIntentRef` / `restoredRef` + `lastOpenSessionRef`. Latches mark
// consumed BEFORE the adapter executes the returned command (spec §2).
// ---------------------------------------------------------------------------

/** Session-reset defaults: an anchor/restore open lands on a SPECIFIC turn, so
 *  it must NOT force atBottom (else a live append yanks the viewport down); a
 *  tail / legacy (null) open keeps the stick-to-bottom default. */
export interface SessionResetCommands { atBottom: boolean; }
/** The one-shot open landing: 'top' → scrollToIndex 0 (align start); 'bottom' →
 *  scrollToIndex last (align end). #285: the 'top' command is inert in the real
 *  browser and Phase A must NOT fix it. */
export interface LandingCommand { target: 'top' | 'bottom'; setAtBottom: boolean; }
/** The restore jump target (a same-session OPEN_CONVERSATION jump). */
export interface RestoreCommand { uuid: string; }

export interface OpenLifecycle {
  /** Reset defaults for a session open. Idempotent per generation (spec §2): a
   *  repeated call for the same generation returns null. */
  sessionChanged(generation: number, intentKind: 'anchor' | 'restore' | 'tail' | null): SessionResetCommands | null;
  /** The one-shot landing. Returns null until `intent != null && itemCount > 0 &&
   *  nodeCount > 0`, then exactly ONCE per generation; re-fires for a NEW one. */
  firstWindowReady(generation: number, intent: 'top' | 'bottom' | null, itemCount: number, nodeCount: number): LandingCommand | null;
  /** The one-shot restore jump — only for a 'restore' intent whose
   *  detailSessionId matches the sessionId; once per generation, re-arms per gen. */
  restoreReady(generation: number, intentKind: string | null, restoreUuid: string | null, detailSessionId: string | null, sessionId: string): RestoreCommand | null;
}

/** Create the open-lifecycle latch set. Pure — the generation is supplied by the
 *  adapter (a ref counter bumped when sessionId changes). */
export function createOpenLifecycle(): OpenLifecycle {
  let resetGen = -1;      // last generation sessionChanged consumed
  let landedGen = -1;     // last generation firstWindowReady landed
  let restoredGen = -1;   // last generation restoreReady fired

  return {
    sessionChanged(generation, intentKind) {
      if (generation === resetGen) return null;   // idempotent per generation
      resetGen = generation;
      // anchor/restore → false; tail / legacy (null) → true.
      return { atBottom: !(intentKind === 'anchor' || intentKind === 'restore') };
    },
    firstWindowReady(generation, intent, itemCount, nodeCount) {
      if (intent == null) return null;                  // an anchor/restore open lands via the jump pipeline
      if (generation === landedGen) return null;        // already landed this open
      if (itemCount <= 0 || nodeCount <= 0) return null; // wait for the first content page + render list
      landedGen = generation;                            // mark consumed BEFORE the command runs (spec §2)
      return intent === 'bottom'
        ? { target: 'bottom', setAtBottom: true }
        : { target: 'top', setAtBottom: false };
    },
    restoreReady(generation, intentKind, restoreUuid, detailSessionId, sessionId) {
      if (intentKind !== 'restore') return null;
      if (generation === restoredGen) return null;      // already restored this open
      if (restoreUuid == null) return null;
      if (detailSessionId == null || detailSessionId !== sessionId) return null; // cross-session transient: keep the pin
      restoredGen = generation;                          // mark consumed BEFORE the dispatch (spec §2)
      return { uuid: restoreUuid };
    },
  };
}

/** Create a paging-gate slice. Construction is SIDE-EFFECT-FREE — it starts no
 *  timers (spec §2 StrictMode contract); the fallback is armed only by
 *  `sessionOpened()`, called from the reader's session-switch effect. */
export function createPagingGates(deps: GateTimerDeps): PagingGates {
  let reverseArmed = false;
  let forwardArmed = false;
  let suppressed = false;       // a programmatic run is in flight (was jumpDrainingRef)
  let currentToken = 0;         // the walk token (was walkTokenRef)
  let fallbackTimer: number | null = null;

  const cancelFallback = (): void => {
    if (fallbackTimer != null) { deps.clearTimeout(fallbackTimer); fallbackTimer = null; }
  };

  return {
    sessionOpened(): void {
      reverseArmed = false;
      forwardArmed = false;
      suppressed = false;
      cancelFallback();
      fallbackTimer = deps.setTimeout(() => {
        reverseArmed = true;
        forwardArmed = true;
        fallbackTimer = null;
      }, ARM_FALLBACK_MS);
    },
    arm(): void {
      reverseArmed = true;
      forwardArmed = true;
      cancelFallback();
    },
    shouldPage(edge: 'start' | 'end'): boolean {
      if (suppressed) return false;
      return edge === 'start' ? reverseArmed : forwardArmed;
    },
    beginProgrammaticRun(): number {
      currentToken += 1;
      suppressed = true;
      return currentToken;
    },
    endProgrammaticRun(token: number): void {
      if (token === currentToken) suppressed = false;
    },
    isCurrentRun(token: number): boolean {
      return token === currentToken;
    },
    dispose(): void {
      cancelFallback();
    },
  };
}

// ---------------------------------------------------------------------------
// B1 — Follow-suspension controller (#285 open-position, spec §4-B1).
//
// CAUSAL MODEL (verified in react-virtuoso 4.18.7 dist): a truthy RAW
// `followOutput` prop — including a FUNCTION that returns false — installs a
// resize watcher that autoscrolls to the LAST item on `SIZE_INCREASED` whenever
// the list has left the bottom, WITHOUT consulting the callback's return value.
// So `() => false` does NOT disable it; only the literal `false` prop does. That
// watcher is exactly what pulled single-page opens to the bottom and made the
// 'top' landing inert. FIX: the reader passes literal `followOutput={false}`
// while this controller reports `'suspended'`, restoring the live callback once
// `'live'`.
//
// The suspension is the OPEN hold and nothing else — a single-page 'top' landing,
// or an anchor/restore open (jump-driven) — released by `settle()` (first
// atBottomStateChange after the landing, a jump landing, or the fallback). A
// multi-page 'bottom' tail open never takes the hold, so live-tail stick stays
// intact from the first frame. OPEN-PATH-ONLY: a settled child-local reveal
// changes no total count and re-applies no Virtuoso props, so it arms no watcher
// (spec §4-B2 established the r2 reveal-suspension would be inert); reveals are
// pinned entirely by SidechainGroup's convergent reassert, not by this controller.
// ---------------------------------------------------------------------------

export type FollowMode = 'live' | 'suspended';

export interface FollowController {
  /** `'suspended'` while the open hold is held, else `'live'`. */
  followMode(): FollowMode;
  /** A session open — the per-open reset. anchor/restore → hold (jump-driven
   *  until settle); tail / legacy (null) → live (a later 'top' landing re-holds). */
  openChanged(intentKind: 'anchor' | 'restore' | 'tail' | null): void;
  /** The one-shot landing resolved: 'top' (single page) → hold until settle;
   *  'bottom' (multi-page tail) → release (stick live). */
  landed(target: 'top' | 'bottom'): void;
  /** Release the OPEN hold — the first atBottomStateChange after the landing, a
   *  jump landing, or the fallback. Idempotent. */
  settle(): void;
}

/** Create the follow-suspension controller. Pure — the reader/adapter drives the
 *  transitions and mirrors `followMode()` into React state. */
export function createFollowController(): FollowController {
  // The OPEN hold: true for a single-page 'top' landing or an anchor/restore open,
  // cleared by settle(). Open-path-only — reveals are pinned by SidechainGroup's
  // convergent reassert, not by follow-suspension (spec §4-B2).
  let openHold = false;

  return {
    followMode: (): FollowMode => (openHold ? 'suspended' : 'live'),
    openChanged: (intentKind): void => {
      // A new open resets the hold: anchor/restore suspend until the jump lands; a
      // tail / legacy open starts live (a subsequent 'top' landing re-holds).
      openHold = intentKind === 'anchor' || intentKind === 'restore';
    },
    landed: (target): void => {
      // 'top' (single page) → hold so the resize watcher can't pull it to the
      // bottom; 'bottom' (multi-page tail) → release so live-tail sticks.
      openHold = target === 'top';
    },
    settle: (): void => {
      openHold = false;
    },
  };
}

// ---------------------------------------------------------------------------
// A4 — Jump-pipeline runner (spec §3-A4).
//
// The ~360-line jump effect body becomes an async runner with named phases:
// draining → resolving → forcing-open → walking → landing → landed | exhausted.
// The machine is PHASE-ORCHESTRATION ONLY (spec F3): ALL DOM work stays behind
// injected deps the adapter supplies. The `waitForQuiesce` loop body, the walk
// assembly, and every landing/DOM closure live in the ADAPTER; the machine owns
// its abort/budget policy and the control flow.
//
// Snapshot-vs-live (spec §3-A4, PRESERVED VERBATIM in Phase A): `captured.*` are
// evaluated over the effect-fire captures (jump/sessionId/detail/nodes/
// virtualFirstItemIndex/hasMore/focusMode-at-start); `live.*` are closures over
// refs read during the run (the walk's per-step target/range, convFindOpen,
// itemRefs/cardRefs, forcedOpenKeys via the re-fire). resolveHit STAYS captured
// (the snapshot-vs-live split is otherwise preserved verbatim); #286 B3 does NOT
// flip it live — instead it gates the no-hit CLEAR on the committed window epoch
// (`resolveExhaustion` over the loadToTarget drain result), so a stale pre-commit
// captured render list can no longer fire a premature exhaustion-clear.
// ---------------------------------------------------------------------------

export type JumpOutcome = 'landed' | 'exhausted-cleared' | 'deferred' | 'aborted';

/** #286 B3 — the loadToTarget drain result (spec §4-B3 / §7 carve-out). The
 *  committed-window-epoch signals the exhaustion decision acks against, so a
 *  stale pre-commit captured render list can never fire a premature clear. */
export interface JumpDrainResult {
  /** The target ended up in the LIVE (hook-internal) window even if the CAPTURED
   *  render list — a possibly-stale pre-commit snapshot — doesn't show it yet. The
   *  next commit will surface it, so this is NEVER a genuine absence (the #286
   *  race site): `found` ⇒ defer, never clear. */
  found: boolean;
  /** The DRAINED edge is genuinely exhausted — directional: `hasPrev` for a
   *  backward drain, `hasMore` for forward (spec F8: never the bottom edge for a
   *  backward jump). A non-exhaustion exit (session change / fetch failure) leaves
   *  this false ⇒ stay pending for a retry re-fire (never a genuine absence). */
  exhausted: boolean;
  /** The rev of the drain's TERMINAL WindowOp — the committed-window epoch the
   *  captured rev must have caught up to before a clear can fire. While
   *  `committedRev < terminalOpRev` a drained page is still in flight to commit, so
   *  the runner defers (pendingExhaustion) and re-evaluates on that commit's
   *  re-fire with fresh captures. */
  terminalOpRev: number;
}

/** #286 B3 — the committed-window-epoch exhaustion gate (pure, spec §4-B3). The
 *  runner's no-hit clear fires ONLY when the target is genuinely absent: it did
 *  NOT reach the live window (`!drain.found`), the DRAINED edge is truly exhausted
 *  (`drain.exhausted`), AND the captured render epoch has caught up to the drain's
 *  terminal op (`committedRev >= drain.terminalOpRev`) so the captures are not a
 *  stale pre-commit snapshot. Every other case DEFERS (pendingExhaustion): a
 *  `found` target awaits its commit; a non-exhausted edge awaits more paging /
 *  a retry; a lagging `committedRev` awaits the terminal op's commit re-fire. */
export function resolveExhaustion(committedRev: number, drain: JumpDrainResult): 'clear' | 'defer' {
  if (drain.found) return 'defer';
  if (!drain.exhausted) return 'defer';
  return committedRev >= drain.terminalOpRev ? 'clear' : 'defer';
}

export interface JumpRunnerDeps {
  /** cancelled || the run token was superseded (the walk-token half via gates). */
  aborted(): boolean;
  /** Drain the window toward the target (the hook's loadToTarget, uuid-bound);
   *  resolves to the committed-window-epoch drain result (#286 B3). */
  loadToTarget(): Promise<JumpDrainResult>;
  /** The captured committed-window rev at effect-fire (`lastOp?.rev`) — the epoch
   *  the exhaustion decision acks the drain's terminal op against (#286 B3). */
  committedRev: number;
  /** The SNAPSHOT column — evaluated over the effect-fire captures. */
  captured: {
    /** findTopLevelNodeFor + nodeVisible over the captured detail (mode/groups
     *  read at call). 'reset-needed' → the turn is coalesced under a non-`all`
     *  focus mode; 'proceed' otherwise. */
    modeHidden(): 'reset-needed' | 'proceed';
    /** nodeIndexForUuid over the CAPTURED nodes + virtualFirstItemIndex. */
    resolveHit(): { arrayIndex: number } | null;
    /** The mounted-hit force-open chain: resolveJumpOwner + ancestorKeys minus
     *  already-forced keys, or null if none is missing. */
    ownerChainToOpen(): string[] | null;
    /** The no-hit fallback chain: the captured detail.items member scan. */
    fallbackChainToOpen(): string[] | null;
  };
  /** The LIVE column — closures over refs, read during the run. */
  live: {
    /** itemRefs/cardRefs/querySelector — is the target's row already mounted? */
    isTargetMounted(): boolean;
    /** walkToTarget assembled adapter-side; 'exhausted' if the walk gave up. */
    walk(): Promise<'mounted' | 'exhausted'>;
    /** waitForQuiesce(targetUuid) — the loop body stays adapter-side. */
    quiesce(): Promise<void>;
    /** The expand_details querySelectorAll force-open (runs before quiesce). */
    openDisclosures(): void;
    /** The #204 card-root landing (scrollNodeIntoView start ×2 + quiesce + mark). */
    landCard(): Promise<void>;
    /** The convergent find-landing reassert (#237 machinery), used for EVERY find
     *  landing so the center survives virtuoso's deferred ResizeObserver re-measure
     *  (#291): reassertCenter re-resolves + re-centers the mark/turn every frame
     *  until stable, then re-marks. */
    landFindReassert(): Promise<void>;
    /** The #236 center landing (center ×2 + quiesce + mark). */
    landCenter(): Promise<void>;
    /** cardRefs.current.has(targetUuid) — picks landCard. */
    hasCardRef(): boolean;
    /** body && el resolvable — the current `result === 'mounted' && body && el`
     *  guard for whether the landing block runs at all. */
    hasLandableElement(): boolean;
    /** convFindOpenRef.current — gates the landFindReassert branch. */
    findOpen(): boolean;
    /** setForcedOpenKeys union of the ancestor chain. */
    requestForceOpen(chain: string[]): void;
    /** dispatch SET_CONV_FOCUS_MODE 'all'. */
    dispatchModeReset(): void;
    /** The landed bookkeeping: arm → flash → pin → cursor → timer →
     *  CLEAR_CONVERSATION_JUMP → forcedOpenKeys reset. */
    landedBookkeeping(arrayIndex: number): void;
    /** dispatch CLEAR_CONVERSATION_JUMP — the exhaustion clear. */
    clearJump(): void;
  };
  /** jump.expand_details (captured) — gates openDisclosures + the reassert branch. */
  expandDetails: boolean;
}

/** Run the jump pipeline. Owns the control flow VERBATIM (same order, same early
 *  returns, an abort check after every await); all DOM work is behind `deps`. */
export async function runJumpPipeline(deps: JumpRunnerDeps): Promise<JumpOutcome> {
  const { captured, live } = deps;

  // ── draining ──────────────────────────────────────────────────────────────
  const drain = await deps.loadToTarget();
  if (deps.aborted()) return 'aborted';

  // ── captured mode-check (reset + defer at most once) ────────────────────────
  // A non-`all` focus mode coalesces the target into a hidden_run marker; reset
  // to `all` so the turn re-renders, then the focusMode re-fire lands it. The
  // reset is one-way (non-`all` → `all`), so this can fire at most once per jump.
  if (captured.modeHidden() === 'reset-needed') {
    live.dispatchModeReset();
    return 'deferred';
  }

  // ── captured resolveHit ─────────────────────────────────────────────────────
  const hit = captured.resolveHit();
  if (hit) {
    // A find-jump into a NESTED subagent MEMBER must force-open the owning
    // ancestor chain FIRST (so the card re-measures to full height); defer to the
    // forcedOpenKeys re-fire that then walks + centers.
    const ownerChain = captured.ownerChainToOpen();
    if (ownerChain) {
      live.requestForceOpen(ownerChain);
      return 'deferred';
    }
    // Warm jump (already mounted) skips the walk; a cold/far jump walks Virtuoso
    // toward the target in mounted-window steps so the path rows measure.
    const result = live.isTargetMounted() ? 'mounted' : await live.walk();
    if (deps.aborted()) return 'aborted';
    // Landing runs only when the walk mounted the target AND its element is
    // resolvable (the current `result === 'mounted' && body && el` guard). An
    // 'exhausted' walk falls through: the flash still identifies the target.
    if (result === 'mounted' && live.hasLandableElement()) {
      if (deps.expandDetails) live.openDisclosures();
      await live.quiesce();
      if (deps.aborted()) return 'aborted';
      if (live.hasCardRef()) {
        await live.landCard();                                  // #204 card root → align top
      } else if (live.findOpen()) {
        await live.landFindReassert();                          // #237 convergent find reassert — survives virtuoso's deferred re-measure (#291)
      } else {
        await live.landCenter();                                // #236 center the turn / mark
      }
    }
    if (deps.aborted()) return 'aborted';
    // Post-landing bookkeeping runs ONLY after the verified final center, on the
    // non-aborted path (arm paging, flash, pin, cursor, clear jump).
    live.landedBookkeeping(hit.arrayIndex);
    return 'landed';
  }

  // ── no-hit: force-open the fallback chain, else defer or clear ──────────────
  const fallbackChain = captured.fallbackChainToOpen();
  if (fallbackChain) {
    live.requestForceOpen(fallbackChain);
    return 'deferred';
  }
  // #286 B3 FIX — the exhaustion clear is gated on the COMMITTED WINDOW EPOCH, not
  // the captured render list (which can be a stale pre-commit snapshot of an
  // in-flight drain — a cold-tail backward jump returns as soon as the top cursor
  // exhausts, potentially BEFORE React commits the prepend that brought the
  // target). Clear ONLY when the target is genuinely absent: it did NOT reach the
  // live window, the DRAINED edge is truly exhausted (directional), and the
  // captured epoch has caught up to the drain's terminal op. Otherwise DEFER
  // (pendingExhaustion): the next committed rev re-fires the runner with fresh
  // captures that either resolve the target (land) or confirm the absence (clear).
  if (resolveExhaustion(deps.committedRev, drain) === 'clear') {
    live.clearJump();
    return 'exhausted-cleared';
  }
  return 'deferred';
}
