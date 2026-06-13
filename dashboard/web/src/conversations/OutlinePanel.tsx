import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { deriveOutline, type OutlineEntry } from './deriveOutline';
import {
  buildOutlineTargets,
  nextTarget,
  outlineTurnVisible,
  type JumpKind,
} from './outlineNavigation';
import type { FocusMode } from './applyFocusMode';
import { fmt } from '../lib/fmt';
import {
  ChatIcon,
  PlanIcon,
  QuestionIcon,
  SubagentIcon,
  ToolGenericIcon,
  WarningIcon,
} from './ConvIcons';
import type { ConversationOutline, OutlineStats, OutlineTurn } from '../types/conversation';

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
function JumpCluster({
  sessionId,
  turns,
  lists,
  currentUuid,
  reduced,
  focusMode,
}: {
  sessionId: string;
  turns: OutlineTurn[];
  lists: JumpLists;
  currentUuid: string | null;
  reduced: boolean;
  focusMode: FocusMode;
}) {
  const { indexByUuid, ...targets } = lists;

  const jump = (kind: JumpKind, dir: 1 | -1, btn: HTMLElement) => {
    const cursor = currentUuid != null && indexByUuid.has(currentUuid) ? indexByUuid.get(currentUuid)! : -1;
    const targetIdx = nextTarget(targets[kind], cursor, dir);
    if (targetIdx == null) {
      if (!reduced) {
        btn.classList.add('conv-pulse-disabled');
        window.setTimeout(() => btn.classList.remove('conv-pulse-disabled'), 300);
      }
      return;
    }
    const turn = turns[targetIdx];
    // Reset to `all` IF the current focus mode would hide the target turn (never
    // a silent jump behind a focus filter); mirrors the entry-click path.
    if (focusMode !== 'all' && !outlineTurnVisible(turn, focusMode)) {
      dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({ type: 'OPEN_CONVERSATION', sessionId, jump: { session_id: sessionId, uuid: turn.uuid } });
  };

  // `label` is the visible chip text; `aria` keeps the descriptive screen-reader
  // phrasing. The error chip's visible label is "error turns" (it navigates the
  // error turns), distinct from the stats-row "14 errors in 13 turns".
  const defs: { kind: JumpKind; glyph: string; label: string; aria: string; key: string }[] = [
    { kind: 'error', glyph: '✕', label: 'error turns', aria: 'error turn', key: 'e' },
    { kind: 'prompt', glyph: '⊕', label: 'prompts', aria: 'prompt', key: 'u' },
    { kind: 'subagent', glyph: '▸', label: 'subagents', aria: 'subagent', key: 'b' },
    { kind: 'plan', glyph: '⊞', label: 'plans', aria: 'plan / question', key: 'p' },
  ];
  const shown = defs.filter((d) => targets[d.kind].length > 0);
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
            title={`Next ${d.aria} (${d.key}) · shift-click for previous`}
            aria-label={`Next ${d.aria}, ${targets[d.kind].length} total`}
            onClick={(ev) => jump(d.kind, ev.shiftKey ? -1 : 1, ev.currentTarget)}
          >
            <span className="conv-jump-cluster-glyph" aria-hidden="true">{d.glyph}</span>
            <span className="conv-jump-cluster-text">{d.label}</span>
            <span className="conv-jump-cluster-count">{targets[d.kind].length}</span>
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
}: {
  stats: OutlineStats;
  errorTurns: number;
}) {
  const yours = stats.turns.human;
  const totalTokens =
    stats.tokens.input +
    stats.tokens.output +
    stats.tokens.cache_creation +
    stats.tokens.cache_read;

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
  const errorPhrase = (() => {
    const n = stats.error_count;
    const base = `${n} ${n === 1 ? 'error' : 'errors'}`;
    return errorTurns !== n ? `${base} in ${errorTurns} turns` : base;
  })();

  return (
    <div className="conv-outline-stats-body">
      <div className="conv-outline-stats-headline">
        <span className="conv-outline-stats-strong">{stats.turns.total}</span> turns
        {' · '}
        <span className="conv-outline-stats-strong">{yours}</span> yours
      </div>
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
      {stats.error_count > 0 && (
        <div className="conv-outline-stat-kv conv-outline-stat-kv--errors">
          <span className="conv-outline-stat-kv-glyph" aria-hidden="true">⚠</span>
          <span className="conv-outline-stat-kv-label">Errors</span>
          <span className="conv-outline-stat-kv-value">{errorPhrase}</span>
        </div>
      )}
    </div>
  );
}

export function OutlinePanel({
  sessionId,
  outline,
}: {
  sessionId: string;
  outline: ConversationOutline | null;
}) {
  // #184 — deliberate full-panel re-render per `convCurrentTurnUuid` tick (the
  // panel rows are trivial, so a subscription-driven render is far simpler than
  // mirroring the reader's imperative aria-current bookkeeping).
  const currentUuid = useSyncExternalStore(
    subscribeStore,
    () => getState().convCurrentTurnUuid,
  );
  const focusMode = useSyncExternalStore(subscribeStore, () => getState().convFocusMode);
  const reduced = useReducedMotion();

  const { entries, sectionByUuid } = useMemo(
    () => (outline ? deriveOutline(outline.turns, outline.subagent_meta)
                   : { entries: [], sectionByUuid: new Map<string, string>() }),
    [outline],
  );

  // #186 §4.3 — lift the shared jump-target builder so the stats card's "N error
  // turns", the error chip count, and the navigation stops are provably one
  // source. `lists.error.length` is the error-TURN count (13), distinct from the
  // server's `error_count` total (14).
  const lists = useMemo(
    () => buildOutlineTargets(outline?.turns ?? []),
    [outline],
  );

  // uuid → OutlineTurn, so a jump can test the target's visibility under the
  // current focus mode before dispatching (spec §5).
  const turnByUuid = useMemo(() => {
    const m = new Map<string, OutlineTurn>();
    (outline?.turns ?? []).forEach((t) => m.set(t.uuid, t));
    return m;
  }, [outline]);

  const jumpTo = (uuid: string) => {
    const turn = turnByUuid.get(uuid);
    if (turn && focusMode !== 'all' && !outlineTurnVisible(turn, focusMode)) {
      dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({
      type: 'OPEN_CONVERSATION',
      sessionId,
      jump: { session_id: sessionId, uuid },
    });
  };

  // The section prompt uuid the scroll-sync cursor currently sits inside (via
  // sectionByUuid). Used to highlight the always-meaningful spine prompt even
  // when the topmost rendered element is a folded fragment or sidechain turn.
  const currentSection = currentUuid != null ? sectionByUuid.get(currentUuid) ?? null : null;

  // Auto-scroll the aria-current entry into view in the panel.
  const listRef = useRef<HTMLOListElement>(null);
  useEffect(() => {
    if (currentUuid == null) return;
    const el = listRef.current?.querySelector<HTMLElement>('[aria-current="true"]');
    el?.scrollIntoView({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' });
  }, [currentUuid, reduced]);

  return (
    <nav className="conv-outline" aria-label="Session outline">
      {outline == null ? (
        <div className="conv-outline-placeholder">Loading outline…</div>
      ) : (
        <>
          <div className="conv-outline-stats">
            <OutlineStatsCard stats={outline.stats} errorTurns={lists.error.length} />
            <JumpCluster
              sessionId={sessionId}
              turns={outline.turns}
              lists={lists}
              currentUuid={currentUuid}
              reduced={reduced}
              focusMode={focusMode}
            />
          </div>
          <ol className="conv-outline-list" ref={listRef}>
            {entries.map((e) => {
              const isCurrent =
                currentUuid != null &&
                // exact landmark match, OR the spine prompt of the current section.
                (e.uuid === currentUuid || (e.type === 'human' && e.uuid === currentSection));
              return (
                <li key={e.entryId}>
                  <button
                    type="button"
                    className={[
                      'conv-outline-entry',
                      `conv-outline-entry--${e.type}`,
                      e.depth ? 'conv-outline-entry--nested' : '',
                      e.error ? 'conv-outline-entry--error' : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    aria-current={isCurrent ? 'true' : undefined}
                    onClick={() => jumpTo(e.uuid)}
                    title={e.label}
                  >
                    <span className="conv-outline-entry-glyph" aria-hidden="true">
                      {entryGlyph(e)}
                    </span>
                    <span className="conv-outline-entry-label">{e.label}</span>
                    {e.thinkingCount > 0 && (
                      <span
                        className="conv-outline-entry-thinking"
                        title={`${e.thinkingCount} thinking ${e.thinkingCount === 1 ? 'block' : 'blocks'}`}
                      >
                        🧠 ×{e.thinkingCount}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </nav>
  );
}
