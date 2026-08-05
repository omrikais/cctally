import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, selectMarkersEnabled, subscribeStore } from '../store/store';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { deriveOutline, type OutlineEntry } from './deriveOutline';
import {
  buildOutlineTargets,
  cursorIndex,
  distinctOwnerTurns,
  nextTargetEntry,
  outlineTurnVisible,
  resolveTurnIndex,
  type JumpKind,
  type OutlineTarget,
} from './outlineNavigation';
import type { FocusMode } from './applyFocusMode';
import { FilesTab } from './FilesTab';
import { fmt, plural, type FmtCtx } from '../lib/fmt';
import {
  ChatIcon,
  PlanIcon,
  QuestionIcon,
  SubagentIcon,
  ToolGenericIcon,
  WarningIcon,
} from './ConvIcons';
import {
  buildConversationJump,
  normalizeConversationRef,
  type CacheRebuild,
  type ConversationOutline,
  type ConversationRefInput,
  type OutlineStats,
  type OutlineTurn,
} from '../types/conversation';

// The shared jump-target lists (built once in OutlinePanel from
// buildOutlineTargets, passed to both the stats card and the jump cluster) — so
// the "13 error turns" number on the Errors row, on the error chip, and the
// navigation stops can never drift.
type JumpLists = ReturnType<typeof buildOutlineTargets>;

// #186 §4.1 — the "Jump to" chip row, now MERGED INTO the stats card (no longer
// a sibling above it). Each chip carries a visible text label beside its count,
// keeps `data-jump-kind` (the reader's e/u/b/p key no-op pulses the matching
// chip via the DOM), and keeps identical jump math (nextTarget over the shared
// lists; click = next, shift-click = previous; miss-pulse, reduced-motion aware).
// The error chip is labeled "error turns" so it never reads as a third
// disagreeing error number beside the "14 errors in 13 turns" line (Codex P2b) —
// it navigates the 13 error TURNS, which is its count.
//
// #463 S4 F-B — that count is `distinctOwnerTurns(lists.error)`, NOT the
// length of the target list. Under landmark awareness the list holds one entry
// per failing CALL, so the length made the chip read `error turns 17` on a
// conversation whose headline read "2 turns". Every other family is one target
// per turn, where the two agree.
function JumpCluster({
  sessionId,
  turns,
  lists,
  currentUuid,
  pinned,
  reduced,
  focusMode,
  markersEnabled,
}: {
  sessionId: ConversationRefInput;
  turns: OutlineTurn[];
  lists: JumpLists;
  currentUuid: string | null;
  // #188 S2 — the explicit-selection pin. Drives the jump-to-next cursor in
  // preference to the scroll-sync `currentUuid` so a repeat forward press steps
  // strictly past where the previous jump LANDED, not past the topmost-visible
  // turn (which sits above a centered target) — closes #187.
  pinned: string | null;
  reduced: boolean;
  focusMode: FocusMode;
  // cache-failure-markers spec §4 — when off, the ⚡ cache chip is suppressed
  // even if flagged turns exist (the opt-out hides ALL marker surfaces).
  markersEnabled: boolean;
}) {
  const qualifiedInput = typeof sessionId !== 'string';
  const conversationRef = normalizeConversationRef(sessionId);
  // #463 S4 remediation C-1 — the WHOLE targets object stays in hand (`lists`).
  // It used to be destructured down to the family lists plus a bare
  // `indexByUuid`, and `resolveTurnIndex` needs all three maps (own uuids,
  // member uuids, segment keys) to resolve the segment key a landmark jump pins.

  // Dispatch the OPEN_CONVERSATION jump for a resolved target, after the
  // focus-mode-unhide check. Shared by both the primary-click jump-to-last and
  // the shift-click step (and the miss-pulse on an empty step).
  //
  // #463 S4 §6.3 — the jump loads `anchorKey`, which for a tier-2 landmark is
  // the SEGMENT holding the failure rather than the top of its turn, while the
  // visibility test still resolves the OWNING turn through `ownerTurnIndex`.
  // `turns` stays the tier-1 array that built these targets, so that index is
  // correct by construction.
  const jumpToTarget = (target: OutlineTarget) => {
    const turn = turns[target.ownerTurnIndex];
    // Reset to `all` IF the current focus mode would hide the target turn (never
    // a silent jump behind a focus filter); mirrors the entry-click path.
    if (turn && focusMode !== 'all' && !outlineTurnVisible(turn, focusMode)) {
      dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef,
      // #463 S4 F-A — the second argument is the segment the target names and
      // the third the block inside it. Round 3 routes this through the shared
      // builder rather than a literal object, so the payload cannot drift from
      // the rail row's or the reader key's.
      jump: buildConversationJump(conversationRef, target.anchorKey, qualifiedInput,
                                  { innerAnchorKey: target.innerAnchorKey }),
    });
  };

  const missPulse = (btn: HTMLElement) => {
    if (!reduced) {
      btn.classList.add('conv-pulse-disabled');
      window.setTimeout(() => btn.classList.remove('conv-pulse-disabled'), 300);
    }
  };

  // #217 S3 E8 — the chip PRIMARY click jumps to the MOST-RECENT occurrence
  // (lists.<kind>.at(-1)) — the keyboard `a`/`L` keys' twin. A direct landing,
  // not a step. Empty family → a graceful no-op (no pulse).
  const jumpToLast = (kind: JumpKind) => {
    const target = lists[kind].at(-1);
    if (target == null) return;
    jumpToTarget(target);
  };

  // The existing next/prev STEPPING (shift-click → previous; the reader's
  // e/E,u/U,b/B,p/P,c/C keys mirror this), cursor-relative with the pin
  // preferred over the lagging scroll cursor (#187).
  const jump = (kind: JumpKind, dir: 1 | -1, btn: HTMLElement) => {
    // #463 S4 remediation C-1 — through the shared `cursorIndex`, which resolves
    // a segment key as well as a turn uuid. The raw `indexByUuid` lookup this
    // replaces missed every pin a landmark jump had left, so the cursor was -1
    // and the chip stepped from the start of the conversation on every press.
    const cursor = cursorIndex(lists, pinned ?? currentUuid);
    const target = nextTargetEntry(lists[kind], cursor, dir);
    if (target == null) { missPulse(btn); return; }
    jumpToTarget(target);
  };

  // `label` is the visible chip text; `aria` keeps the descriptive screen-reader
  // phrasing. The error chip's visible label is "error turns" (it navigates the
  // error turns), distinct from the stats-row "14 errors in 13 turns".
  const defs: { kind: JumpKind; glyph: string; label: string; aria: string; key: string }[] = [
    { kind: 'error', glyph: '✕', label: 'error turns', aria: 'error turn', key: 'e' },
    { kind: 'prompt', glyph: '⊕', label: 'prompts', aria: 'prompt', key: 'u' },
    { kind: 'subagent', glyph: '▸', label: 'subagents', aria: 'subagent', key: 'b' },
    { kind: 'plan', glyph: '⊞', label: 'plans', aria: 'plan / question', key: 'p' },
    // cache-failure-markers spec §4 — amber ⚡ cache-rebuilds jump chip, `c` key.
    { kind: 'cache', glyph: '⚡', label: 'cache rebuilds', aria: 'cache rebuild', key: 'c' },
    // #217 S3 F8 — compaction landmark jump chip, `m` key (compaction summary).
    { kind: 'compaction', glyph: '⊟', label: 'compaction', aria: 'compaction', key: 'm' },
    // #217 S6 F4 — bookmark jump chip, `i` key. No markers gate (the
    // d.kind !== 'cache' clause already admits it); shown only when there are
    // bookmark targets.
    { kind: 'bookmark', glyph: '★', label: 'bookmarks', aria: 'bookmark', key: 'i' },
  ];
  // A def is shown when it has targets AND (for cache) markers are on — the
  // opt-out suppresses the chip even when flagged turns exist.
  const shown = defs.filter(
    (d) => lists[d.kind].length > 0 && (d.kind !== 'cache' || markersEnabled),
  );
  // ONLY the error chip counts turns, because only its label says "turns". A
  // chip labelled "plans" is read as how many plans there are, and a Codex
  // conversation with nine plan landmarks in two turns has nine; answering 2
  // there would be the same conflation with the words exchanged. The residual
  // §8.2 already records — stepping is turn-granular, so nine plan rows are two
  // shift-click stops — is unchanged by either count.
  const stops = (kind: JumpKind) =>
    (kind === 'error' ? distinctOwnerTurns(lists[kind]) : lists[kind].length);
  if (shown.length === 0) return null;
  return (
    <div className="conv-outline-jump">
      <div className="conv-outline-jump-label">Jump to</div>
      <div className="conv-jump-cluster" role="group" aria-label="Jump to next landmark">
        {shown.map((d) => (
          <button
            key={d.kind}
            type="button"
            className="conv-jump-cluster-btn"
            data-jump-kind={d.kind}
            // #217 S3 E8 — primary click jumps to the LATEST occurrence;
            // shift-click steps to the previous one (the reader keys mirror both).
            title={`Latest ${d.aria} (${d.key}) · shift-click for previous`}
            aria-label={`Latest ${d.aria}, ${stops(d.kind)} total`}
            onClick={(ev) =>
              ev.shiftKey ? jump(d.kind, -1, ev.currentTarget) : jumpToLast(d.kind)
            }
          >
            <span className="conv-jump-cluster-glyph" aria-hidden="true">{d.glyph}</span>
            <span className="conv-jump-cluster-text">{d.label}</span>
            <span className="conv-jump-cluster-count">{stops(d.kind)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// #177 S5 §3 / #186 §4 — the outline sidebar + the "session at a glance" card.
// Renders as a third grid column in `.conv-view`. Top: the merged stats card
// (headline + stat tiles + labeled distribution rows + the in-card jump
// cluster). Below: the navigable landmark list from `deriveOutline`,
// independently scrollable. Scroll-sync: the reader's IntersectionObserver
// writes `convCurrentTurnUuid`; this panel highlights the SECTION PROMPT whose
// section contains that uuid (via `sectionByUuid`) plus the exact landmark entry
// when the uuid matches one.

// Per-entry leading glyph. Errors win, then plan/question, then per-type marks.
// Every glyph is aria-hidden (the label text carries the meaning).
function entryGlyph(e: OutlineEntry) {
  if (e.error) return <WarningIcon />;
  if (e.plan) return <PlanIcon />;
  if (e.question) return <QuestionIcon />;
  switch (e.type) {
    case 'human': return <ChatIcon />;
    case 'subagent': return <SubagentIcon />;
    case 'error': return <WarningIcon />;
    case 'plan': return <PlanIcon />;
    case 'question': return <QuestionIcon />;
    case 'heading': return <ToolGenericIcon />;
    // cache-failure-markers spec §4 — standalone cache landmark leads with an
    // amber ⚡ (the .conv-outline-entry--cache rule paints it amber). Other
    // types that merely COINCIDE keep their own leading glyph (error/plan win);
    // the trailing ⚡ suffix below carries the cache flag on those rows.
    case 'cache': return <span className="conv-outline-entry-cache-glyph">⚡</span>;
    // #217 S3 F8 — compaction landmark leads with a distinct glyph (the
    // .conv-outline-entry--compaction rule styles the row).
    case 'compaction': return <span className="conv-outline-entry-compaction-glyph" aria-hidden="true">⊟</span>;
    // #217 S5 F7 — the terminal "session complete" landmark leads with a check.
    case 'completion': return <span className="conv-outline-entry-completion-glyph" aria-hidden="true">✓</span>;
    // #217 S6 F4 — a bookmark landmark leads with a ★ (the
    // .conv-outline-entry--bookmark rule paints it the amber accent).
    case 'bookmark': return <span className="conv-outline-entry-bookmark-glyph" aria-hidden="true">★</span>;
    default: return <ToolGenericIcon />;
  }
}

// #186 §4.2/§4.3 — the merged "session at a glance" card (Variant B). Headline
// (turns/yours), three bounded stat tiles (Time/Tokens/Cost), labeled
// distribution rows (Models/Tools/Errors), and the reconciled error count.
// `errorTurns` is the count of turns carrying an error (from the shared
// buildOutlineTargets lists) — distinct from `stats.error_count`, the server
// total; the row appends " in N turns" ONLY when they differ.
function OutlineStatsCard({
  stats,
  errorTurns,
  markersEnabled,
}: {
  stats: OutlineStats;
  errorTurns: number;
  // cache-failure-markers spec §4 — the opt-out: when off, the "Cache" KV row
  // is suppressed even when stats.cache_failures.count > 0.
  markersEnabled: boolean;
}) {
  const yours = stats.turns.human;
  const totalTokens =
    stats.tokens.input +
    stats.tokens.output +
    (stats.tokens.source === 'codex'
      ? (stats.tokens.cached_input ?? 0) + (stats.tokens.reasoning_output ?? 0)
      : stats.tokens.cache_creation + stats.tokens.cache_read);

  // Tool histogram: top-3 by count (descending), then `+N more`. The full sorted
  // list goes into the title tooltip so nothing is lost at a glance.
  const toolPairs = useMemo(
    () => Object.entries(stats.tool_counts).sort((a, b) => b[1] - a[1]),
    [stats.tool_counts],
  );
  const topTools = toolPairs.slice(0, 3);
  const moreCount = toolPairs.length - topTools.length;
  const toolTitle = toolPairs.map(([name, n]) => `${name} ×${n}`).join('\n');

  const modelPairs = useMemo(
    () => Object.entries(stats.models).sort((a, b) => b[1] - a[1]),
    [stats.models],
  );

  // The reconciled error phrase: "14 errors in 13 turns" when the server total
  // exceeds the error-turn count; "5 errors" when they agree.
  //
  // #463 S4 §6.5 — THREE states, because `0` and `null` are different claims.
  // A positive count renders the row; `0` HIDES it, matching the Claude side,
  // because hiding is not a claim; `null` renders an explicit declined state,
  // because rendering `0` would assert an absence nobody proved. The server
  // sends null when a conversation has outcome-bearing rows and no verdict on
  // any of them — its retained event payloads are gone or unparseable.
  const errorPhrase = (() => {
    const n = stats.error_count;
    if (n == null) return 'not reported';
    const base = plural(n, 'error');
    return errorTurns !== n ? `${base} in ${plural(errorTurns, 'turn')}` : base;
  })();

  return (
    <div className="conv-outline-stats-body">
      <div className="conv-outline-stats-headline">
        <span className="conv-outline-stats-strong">{stats.turns.total}</span>
        {stats.turns.total === 1 ? ' turn' : ' turns'}
        {' · '}
        <span className="conv-outline-stats-strong">{yours}</span> yours
      </div>
      {stats.tokens.source === 'codex' && (
        <div className="conv-outline-stat-kv conv-outline-stat-kv--tokens" title="Provider-native Codex token fields">
          <span className="conv-outline-stat-kv-glyph" aria-hidden="true">#</span>
          <span className="conv-outline-stat-kv-label">Tokens</span>
          <span className="conv-outline-stat-kv-value">
            in {fmt.tokens(stats.tokens.input)} · out {fmt.tokens(stats.tokens.output)} · cached in {fmt.tokens(stats.tokens.cached_input ?? 0)} · reasoning out {fmt.tokens(stats.tokens.reasoning_output ?? 0)}
          </span>
        </div>
      )}
      <div className="conv-outline-stat-tiles">
        <div className="conv-outline-stat-tile">
          <span className="conv-outline-stat-tile-value">{fmt.hhmm(stats.duration_seconds)}</span>
          <span className="conv-outline-stat-tile-label">Time</span>
        </div>
        <div className="conv-outline-stat-tile conv-outline-stat-tile--tokens">
          <span className="conv-outline-stat-tile-value">{fmt.tokens(totalTokens)}</span>
          <span className="conv-outline-stat-tile-label">Tokens</span>
        </div>
        <div className="conv-outline-stat-tile conv-outline-stat-tile--cost">
          <span className="conv-outline-stat-tile-value">{fmt.usd2(stats.cost_usd)}</span>
          <span className="conv-outline-stat-tile-label">Cost</span>
        </div>
      </div>
      {modelPairs.length > 0 && (
        <div className="conv-outline-stat-kv conv-outline-stat-kv--models">
          <span className="conv-outline-stat-kv-glyph" aria-hidden="true">⬡</span>
          <span className="conv-outline-stat-kv-label">Models</span>
          <span className="conv-outline-stat-kv-value">
            {modelPairs.map(([m, n]) => `${m} ×${n}`).join(', ')}
          </span>
        </div>
      )}
      {topTools.length > 0 && (
        <div className="conv-outline-stat-kv conv-outline-stat-kv--tools" title={toolTitle}>
          <span className="conv-outline-stat-kv-glyph" aria-hidden="true">⚙</span>
          <span className="conv-outline-stat-kv-label">Tools</span>
          <span className="conv-outline-stat-kv-value">
            {topTools.map(([name, n]) => `${name} ×${n}`).join('  ')}
            {moreCount > 0 ? `  +${moreCount} more` : ''}
          </span>
        </div>
      )}
      {(stats.error_count == null || stats.error_count > 0) && (
        <div
          className={`conv-outline-stat-kv conv-outline-stat-kv--errors${
            stats.error_count == null ? ' conv-outline-stat-kv--declined' : ''}`}
          title={stats.error_count == null
            ? 'This conversation has tool results, but nothing that could say whether any of them failed.'
            : undefined}
        >
          <span className="conv-outline-stat-kv-glyph" aria-hidden="true">⚠</span>
          <span className="conv-outline-stat-kv-label">Errors</span>
          <span className="conv-outline-stat-kv-value">{errorPhrase}</span>
        </div>
      )}
      {/* cache-failure-markers spec §4 — "Cache" KV row, rendered ONLY when
          count > 0 (unlike the always-on Errors row) AND markers are on. ~65%
          of sessions have zero rebuilds, so a perpetual "0 rebuilds" would be
          exactly the clutter to avoid. */}
      {markersEnabled && stats.cache_failures && stats.cache_failures.count > 0 && (
        <div className="conv-outline-stat-kv conv-outline-stat-kv--cache">
          <span className="conv-outline-stat-kv-glyph" aria-hidden="true">⚡</span>
          <span className="conv-outline-stat-kv-label">Cache</span>
          <span className="conv-outline-stat-kv-value">
            {stats.cache_failures.count} {stats.cache_failures.count === 1 ? 'rebuild' : 'rebuilds'}
            {' · ~'}{fmt.usd2(stats.cache_failures.est_wasted_usd)}
          </span>
        </div>
      )}
    </div>
  );
}

// #217 S6 F3 — the per-rebuild jump list that expands the scalar "Cache · N
// rebuilds · ~$X" stat into a navigable list (templated on the session modal's
// CacheRebuildsSection rows). Fed entirely from props (no global access): the
// worst-first `rebuilds[]` already on the wire, a uuid→OutlineTurn map for the
// human label (falling back to the indexed "turn N" — #226 — when the turn has
// no prose label, and to a bare "turn" only when the uuid isn't in the skeleton
// at all), a uuid→skeleton-index map for that fallback, and the display-tz
// fmtCtx for the rebuild time. Caps at 3 with a "+N more" expander, like the
// modal. Each row's primary action is the same OPEN_CONVERSATION jump every
// other outline jump uses; a stale uuid jumps as a graceful no-op.
function OutlineCacheRebuilds({
  rebuilds, turnByUuid, indexByUuid, fmtCtx, onJump,
}: {
  rebuilds: CacheRebuild[];
  turnByUuid: Map<string, OutlineTurn>;
  indexByUuid: Map<string, number>;
  fmtCtx: FmtCtx;
  // #463 S4 remediation — the rail's ONE jump dispatcher. This list used to
  // build its own OPEN_CONVERSATION inline, which made it the single rail row
  // that skipped the focus-mode unhide check every other rail row performs: a
  // click while a focus mode hid the flagged turn loaded a key mounted nowhere
  // and the jump exhausted silently. Same payload, one code path.
  onJump: (uuid: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const CAP = 3;
  const shown = expanded ? rebuilds : rebuilds.slice(0, CAP);
  return (
    <div className="conv-outline-rebuilds">
      <ul className="conv-rebuild-list">
        {shown.map((r) => {
          // Prefer the turn's prose label; else the 1-based skeleton index
          // ("turn N", #226) when the uuid IS in the skeleton; else a bare
          // "turn" for a genuinely-stale uuid with no index.
          const idx = indexByUuid.get(r.uuid);
          const label = turnByUuid.get(r.uuid)?.label || (idx != null ? `turn ${idx + 1}` : 'turn');
          return (
            <li key={r.uuid}>
              <button
                type="button"
                className="conv-rebuild-jump"
                aria-label={`Jump to cache rebuild: ~${fmt.usd2(r.est_wasted_usd)} wasted`}
                onClick={() => onJump(r.uuid)}
              >
                <span className="rb-cost">{fmt.usd2(r.est_wasted_usd)}</span>
                <span className="rb-label">{label}</span>
                <span className="rb-time">{r.ts ? fmt.timeHHmm(r.ts, fmtCtx, { noSuffix: true }) : ''}</span>
                {r.subagent_key ? <span className="rb-sub">subagent</span> : null}
              </button>
            </li>
          );
        })}
      </ul>
      {rebuilds.length > CAP && !expanded && (
        <button type="button" className="conv-rebuild-more" onClick={() => setExpanded(true)}>
          +{rebuilds.length - CAP} more
        </button>
      )}
    </div>
  );
}

export function OutlinePanel({
  sessionId,
  outline,
}: {
  sessionId: ConversationRefInput;
  outline: ConversationOutline | null;
}) {
  const qualifiedInput = typeof sessionId !== 'string';
  const conversationRef = normalizeConversationRef(sessionId);
  // #184 — deliberate full-panel re-render per `convCurrentTurnUuid` tick (the
  // panel rows are trivial, so a subscription-driven render is far simpler than
  // mirroring the reader's imperative aria-current bookkeeping).
  const currentUuid = useSyncExternalStore(
    subscribeStore,
    () => getState().convCurrentTurnUuid,
  );
  // #188 S2 — the explicit-selection pin. When set it drives aria-current
  // (exact, no section fallback) + the jump-to-next cursor; null falls back to
  // today's scroll-sync behavior.
  const pinned = useSyncExternalStore(subscribeStore, () => getState().convPinnedUuid);
  // #463 S4 remediation C-3 — the inner anchor that pin landed on, when the
  // jump named one. Written by the reader's landing bookkeeping for EVERY
  // entry point, which is what lets the rail mark one landmark row rather than
  // every row sharing the segment.
  const pinnedAnchorKey = useSyncExternalStore(
    subscribeStore, () => getState().convPinnedAnchorKey);
  const focusMode = useSyncExternalStore(subscribeStore, () => getState().convFocusMode);
  // #217 S5 F2 — the [Outline] [Files] tab selection (transient per-session).
  const outlineTab = useSyncExternalStore(subscribeStore, () => getState().convOutlineTab);
  // cache-failure-markers spec §4 — the opt-out, threaded into deriveOutline
  // (skips cache curation entirely when off) and into the stats row + jump chip.
  const markersEnabled = useSyncExternalStore(subscribeStore, () =>
    selectMarkersEnabled(getState()),
  );
  // #217 S6 F4 — the current session's bookmarks, threaded into deriveOutline (★
  // landmarks) and buildOutlineTargets (the ★ jump list). The panel re-renders
  // per store tick, so a bookmark toggle live-updates the outline.
  const bookmarks = useSyncExternalStore(subscribeStore, () => getState().convBookmarks);
  const reduced = useReducedMotion();
  // #217 S6 F3 — display-tz fmtCtx for the cache-rebuild list times. Same source
  // the reader uses (the server resolves "local" before the envelope leaves
  // Python); panel-level subscription is fine (the panel re-renders per tick).
  const display = useDisplayTz();
  const fmtCtx = useMemo<FmtCtx>(
    () => ({ tz: display.resolvedTz, offsetLabel: display.offsetLabel }),
    [display.resolvedTz, display.offsetLabel],
  );

  const { entries, sectionByUuid } = useMemo(
    () => (outline ? deriveOutline(outline.turns, outline.subagent_meta, markersEnabled,
                                   outline.task_completion, bookmarks, outline.landmarks)
                   : { entries: [], sectionByUuid: new Map<string, string>() }),
    [outline, markersEnabled, bookmarks],
  );

  // #217 S3 E6(a) — the display-only per-subagent cost map (subagent_key → USD).
  // Rendered on each subagent entry's row; covers buckets with empty
  // subagent_meta too (the server emits a cost for every bucket).
  const subagentCosts = outline?.subagent_costs ?? {};

  // #186 §4.3 — lift the shared jump-target builder so the stats card's "N error
  // turns", the error chip count, and the navigation stops are provably one
  // source. #463 S4 F-B — the error-TURN count is
  // `distinctOwnerTurns(lists.error)`, because a landmark-aware `lists.error`
  // holds one entry per failing CALL; `lists.error.length` is the server's
  // `error_count` by construction, which made the reconciliation phrase
  // unreachable for Codex.
  const lists = useMemo(
    () => buildOutlineTargets(outline?.turns ?? [], bookmarks, outline?.landmarks),
    [outline, bookmarks],
  );

  // uuid → OutlineTurn, so a jump can test the target's visibility under the
  // current focus mode before dispatching (spec §5).
  const turnByUuid = useMemo(() => {
    const m = new Map<string, OutlineTurn>();
    (outline?.turns ?? []).forEach((t) => m.set(t.uuid, t));
    return m;
  }, [outline]);

  // #226 — skeleton index per uuid, for the cache-rebuild label fallback when a
  // flagged turn has no prose label (`_outline_label` → '' for a tool-only
  // turn). Mirrors deriveOutline's `turn ${turnIndex + 1}` idiom.
  const indexByUuid = useMemo(() => {
    const m = new Map<string, number>();
    (outline?.turns ?? []).forEach((t, i) => m.set(t.uuid, i));
    return m;
  }, [outline]);

  // The rail's ONE jump dispatcher: every row in this panel — an outline entry,
  // a Files-tab file or touch, a cache-rebuild row — goes through it, so the
  // focus-mode unhide check cannot be true of some rows and not others.
  //
  // #463 S4 F-C — the owning turn is resolved through `resolveTurnIndex`, not
  // through a map of tier-1 uuids. Several callers pass a SEGMENT key: a tier-2
  // landmark row, and a Codex file touch, whose anchor is the segment holding
  // the change. A tier-1 lookup returned `undefined` for both, the `turn &&`
  // guard short-circuited, and the focus-mode reset never ran — so clicking a
  // reasoning landmark while focus mode was `edits` loaded a key mounted
  // nowhere and the jump exhausted silently. `resolveTurnIndex` consults the
  // own-uuid map, then member uuids, then segment keys.
  const jumpTo = (uuid: string, innerAnchorKey?: string) => {
    const ownerIndex = resolveTurnIndex(lists, uuid);
    const turn = ownerIndex !== undefined ? (outline?.turns ?? [])[ownerIndex] : undefined;
    if (turn && focusMode !== 'all' && !outlineTurnVisible(turn, focusMode)) {
      dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef,
      jump: buildConversationJump(conversationRef, uuid, qualifiedInput, { innerAnchorKey }),
    });
  };

  // The set of uuids that are THEMSELVES outline entries (prompts + curated
  // landmarks, including subagent bucket-root uuids). Used to suppress the
  // section-prompt fallback below when the cursor already matches an exact entry.
  const entryUuids = useMemo(() => new Set(entries.map((e) => e.uuid)), [entries]);

  // #463 S4 F-G, reworked by the remediation (C-3) — WHICH ROW the rail marks.
  // Several tier-2 rows can share one segment anchor, so the uuid alone marked
  // all of them. F-G answered that by remembering the entry the rail's own row
  // click named, which left the highlight behind on a chip jump or a keyboard
  // jump — the rail was the only writer of that memory. The answer now comes
  // from the pin itself, which the reader writes when a jump LANDS and which
  // carries the inner anchor that jump aligned. One mechanism, and it serves
  // whichever entry point issued the jump.
  //
  // An inner-anchor pin names one exact tier-2 landmark. A bare pin names only
  // the landed item, so it may select a tier-1 row with that uuid but must not
  // claim a finer landmark inside the item. If the landed item has no row of
  // its own, resolve the nearest semantic ancestor: its subagent bucket first,
  // then its enclosing prompt. This keeps aria-current truthful for saved
  // positions and for jumps to generic/sidechain members (#492).
  const activeEntryId = useMemo(() => {
    if (pinned == null) return null;
    const owned = entries.filter((e) => e.uuid === pinned);
    if (pinnedAnchorKey != null) {
      return (owned.find((e) => e.innerAnchorKey === pinnedAnchorKey) ?? owned[0])
        ?.entryId ?? null;
    }
    const direct = owned.find((e) => !e.landmark);
    if (direct) return direct.entryId;

    const ownerIndex = resolveTurnIndex(lists, pinned);
    const owner = ownerIndex != null ? outline?.turns[ownerIndex] : undefined;
    if (owner?.subagent_key != null) {
      const bucket = entries.find(
        (e) => e.type === 'subagent' && e.subagentKey === owner.subagent_key,
      );
      if (bucket) return bucket.entryId;
    }

    const sectionUuid = sectionByUuid.get(pinned)
      ?? (owner ? sectionByUuid.get(owner.uuid) : undefined);
    return entries.find((e) => e.type === 'human' && e.uuid === sectionUuid)?.entryId ?? null;
  }, [entries, lists, outline?.turns, pinned, pinnedAnchorKey, sectionByUuid]);

  // The section prompt uuid the scroll-sync cursor currently sits inside (via
  // sectionByUuid). Used to highlight the always-meaningful spine prompt when the
  // topmost rendered element is a folded fragment or sidechain turn that is NOT
  // itself a landmark. #192 — gate on the cursor NOT being an entry: when it IS
  // (a subagent / heading / plan landmark, e.g. a trailing subagent card that
  // stays topmost-visible after a scroll), the exact-match clause already lights
  // that entry, so resolving a section here would ALSO light the spine prompt —
  // the user-reported double-mark. A non-entry cursor (generic prose / folded
  // fragment / sidechain member) still resolves its section prompt as before.
  const currentSection =
    currentUuid != null && !entryUuids.has(currentUuid)
      ? sectionByUuid.get(currentUuid) ?? null
      : null;

  // Auto-scroll the aria-current entry into view in the panel. Keyed on the
  // effective selection (pin wins over the scroll-sync cursor) so a pin change —
  // e.g. an outline click that pins exactly the clicked entry — re-scrolls the
  // panel to it (#188 S2).
  const listRef = useRef<HTMLOListElement>(null);
  const effectiveUuid = pinned ?? currentUuid;
  useEffect(() => {
    if (effectiveUuid == null) return;
    const el = listRef.current?.querySelector<HTMLElement>('[aria-current="true"]');
    el?.scrollIntoView({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' });
  }, [effectiveUuid, reduced]);

  return (
    <nav className="conv-outline" aria-label="Session outline">
      {outline == null ? (
        <div className="conv-outline-placeholder">Loading outline…</div>
      ) : (
        <>
          <div className="conv-outline-stats">
            <OutlineStatsCard stats={outline.stats} errorTurns={distinctOwnerTurns(lists.error)} markersEnabled={markersEnabled} />
            {/* #217 S6 F3 — expand the scalar Cache stat into a per-rebuild jump
                list. Gated on the same markersEnabled opt-out + count > 0 as the
                stats row above it. */}
            {markersEnabled && outline.stats.cache_failures && outline.stats.cache_failures.count > 0 && (
              <OutlineCacheRebuilds
                rebuilds={outline.stats.cache_failures.rebuilds}
                turnByUuid={turnByUuid}
                indexByUuid={indexByUuid}
                fmtCtx={fmtCtx}
                onJump={jumpTo}
              />
            )}
            <JumpCluster
              sessionId={sessionId}
              turns={outline.turns}
              lists={lists}
              currentUuid={currentUuid}
              pinned={pinned}
              reduced={reduced}
              focusMode={focusMode}
              markersEnabled={markersEnabled}
            />
          </div>
          {/* #217 S5 F2 — [Outline] [Files] tab toggle. The Files count rides on
              the tab so an empty-files session still shows a "0" affordance. */}
          <div className="conv-outline-tabs" role="tablist" aria-label="Outline view">
            <button
              type="button"
              role="tab"
              className="conv-outline-tab"
              aria-selected={outlineTab === 'outline'}
              onClick={() => dispatch({ type: 'SET_CONV_OUTLINE_TAB', tab: 'outline' })}
            >
              Outline
            </button>
            <button
              type="button"
              role="tab"
              className="conv-outline-tab"
              aria-selected={outlineTab === 'files'}
              onClick={() => dispatch({ type: 'SET_CONV_OUTLINE_TAB', tab: 'files' })}
            >
              Files
              <span className="conv-outline-tab-count" aria-hidden="true">
                {(outline.files ?? []).length + (outline.provider_files ?? []).length}
              </span>
            </button>
          </div>
          {outlineTab === 'files' ? (
            <div className="conv-outline-files" role="tabpanel">
              <FilesTab
                files={outline.files ?? []}
                providerFiles={outline.provider_files ?? []}
                onJump={jumpTo}
              />
            </div>
          ) : (
          <ol className="conv-outline-list" ref={listRef}>
            {entries.map((e) => {
              // #188 S2 — the explicit pin wins: when set, aria-current is the
              // EXACT pinned entry (no section fallback), so an outline click —
              // including a subagent click, whose entry uuid is its bucket-root
              // uuid — selects precisely what was clicked (Bugs 2/3). Without a
              // pin, fall back to today's scroll-sync behavior (exact landmark
              // match OR the spine prompt of the current section).
              const isCurrent =
                pinned != null
                  ? activeEntryId != null && e.entryId === activeEntryId
                  : currentUuid != null &&
                    (e.uuid === currentUuid || (e.type === 'human' && e.uuid === currentSection));
              // #217 S3 E6(c) — tree indentation. depth 0 = spine, 1 = section
              // landmark, ≥2 = a nested sub-subagent. A CSS var drives the
              // left-pad per level so a deeper child reads as indented under its
              // parent; data-depth lets tests assert the level without pixels.
              return (
                <li key={e.entryId}>
                  <button
                    type="button"
                    className={[
                      'conv-outline-entry',
                      `conv-outline-entry--${e.type}`,
                      e.depth ? 'conv-outline-entry--nested' : '',
                      // ≥2 = a tree child; drives the extra-indent rule.
                      e.depth >= 2 ? 'conv-outline-entry--subnested' : '',
                      // #463 S4 §6.7 — a tier-2 landmark row. It is already a
                      // button (the row-to-cell-button pattern this list has
                      // always used), so this only carries the indent rule and
                      // the wrapping label; the severity colour comes from the
                      // existing `--error` modifier below.
                      e.landmark ? 'conv-outline-entry--landmark' : '',
                      e.error ? 'conv-outline-entry--error' : '',
                      // cache-failure-markers spec §4 — flagged rows (standalone
                      // OR coinciding) take the cache modifier for the amber cue.
                      e.cache ? 'conv-outline-entry--cache-flagged' : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    data-depth={e.depth}
                    style={e.depth >= 2 ? ({ ['--conv-outline-depth' as string]: String(e.depth) }) : undefined}
                    aria-current={isCurrent ? 'true' : undefined}
                    onClick={() => jumpTo(e.uuid, e.innerAnchorKey)}
                    title={e.label}
                  >
                    <span className="conv-outline-entry-glyph" aria-hidden="true">
                      {entryGlyph(e)}
                    </span>
                    <span className="conv-outline-entry-label">{e.label}</span>
                    {/* #217 S3 E6(a) — per-subagent cost, display-only. Shown on a
                        subagent entry when the server emitted a cost for its
                        bucket (covers empty-subagent_meta buckets too). The cost
                        lookup is inlined here (#218/#219 I-2 P3 cosmetic): it was
                        computed for every entry but only used in this branch. */}
                    {e.type === 'subagent' && e.subagentKey != null && subagentCosts[e.subagentKey] != null && (
                      <span
                        className="conv-outline-entry-cost"
                        title={`Subagent cost (display-only): ${fmt.usd2(subagentCosts[e.subagentKey])}`}
                      >
                        {fmt.usd2(subagentCosts[e.subagentKey])}
                      </span>
                    )}
                    {e.thinkingCount > 0 && (
                      <span
                        className="conv-outline-entry-thinking"
                        title={`${e.thinkingCount} thinking ${e.thinkingCount === 1 ? 'block' : 'blocks'}`}
                      >
                        🧠 ×{e.thinkingCount}
                      </span>
                    )}
                    {/* cache-failure-markers spec §4 — trailing amber ⚡ suffix on
                        a row that COINCIDES with another landmark (its leading
                        glyph stays its own type's). The standalone 'cache' entry
                        already leads with ⚡, so skip the redundant suffix there. */}
                    {e.cache && e.type !== 'cache' && (
                      <span
                        className="conv-outline-entry-cache"
                        title={
                          e.cacheInfo
                            ? `Cache rebuilt — ${fmt.compact(e.cacheInfo.tokens_recreated, { upper: true })} re-created (~${fmt.usd2(e.cacheInfo.est_wasted_usd)} extra)`
                            : 'Cache rebuilt'
                        }
                      >
                        ⚡
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ol>
          )}
        </>
      )}
    </nav>
  );
}
