import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { deriveOutline, type OutlineEntry } from './deriveOutline';
import { buildOutlineTargets, nextTarget, outlineTurnVisible, type JumpKind } from './outlineNavigation';
import type { FocusMode } from './applyFocusMode';
import { fmt } from '../lib/fmt';
import {
  ChatIcon,
  PlanIcon,
  QuestionIcon,
  SubagentIcon,
  SystemIcon,
  ThinkingIcon,
  ToolGenericIcon,
  WarningIcon,
} from './ConvIcons';
import type { ConversationOutline, OutlineStats, OutlineTurn } from '../types/conversation';

// #177 S5 §4 — the jump-to-next glyph cluster. Four buttons (error / prompt /
// subagent / plan-or-question), each shown only when its target count > 0,
// rendered as `glyph + count`. Click = next, shift-click = previous. The cursor
// resolves from `convCurrentTurnUuid` (the scroll-sync turn) → -1 ("before the
// start") when absent. A miss pulses the button (reduced-motion: no pulse). The
// reader's e/u/b/p keys share the same `buildOutlineTargets` + `nextTarget` math
// (outlineNavigation.ts — #184 lifted the duplicated builder there); this cluster
// carries `data-jump-kind` so the reader's key no-op can pulse the matching
// button via the DOM.

function JumpCluster({
  sessionId,
  outline,
  currentUuid,
  reduced,
  focusMode,
}: {
  sessionId: string;
  outline: ConversationOutline;
  currentUuid: string | null;
  reduced: boolean;
  focusMode: FocusMode;
}) {
  const turns = outline.turns;
  // #184 — shared jump-target builder (lists + uuid→index map) so the panel
  // cluster and the reader keys can never drift apart.
  const { indexByUuid, ...lists } = useMemo(() => buildOutlineTargets(turns), [turns]);

  const jump = useCallback((kind: JumpKind, dir: 1 | -1, btn: HTMLElement) => {
    const cursor = currentUuid != null && indexByUuid.has(currentUuid) ? indexByUuid.get(currentUuid)! : -1;
    const targetIdx = nextTarget(lists[kind], cursor, dir);
    if (targetIdx == null) {
      if (!reduced) {
        btn.classList.add('conv-pulse-disabled');
        window.setTimeout(() => btn.classList.remove('conv-pulse-disabled'), 300);
      }
      return;
    }
    const turn = turns[targetIdx];
    // Reset to `all` IF the current focus mode would hide the target turn
    // (spec §5: never a silent jump behind a focus filter). The store reducer
    // no longer blanket-resets on same-session OPEN_CONVERSATION, so the
    // per-jump check is the authority — mirror the reader's jumpNext, but over
    // the OutlineTurn skeleton (the panel has no RenderNode) via the cheap
    // outlineTurnVisible twin of nodeVisible.
    if (focusMode !== 'all' && !outlineTurnVisible(turn, focusMode)) {
      dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({ type: 'OPEN_CONVERSATION', sessionId, jump: { session_id: sessionId, uuid: turn.uuid } });
  }, [lists, indexByUuid, currentUuid, turns, sessionId, reduced, focusMode]);

  const defs: { kind: JumpKind; glyph: string; label: string; key: string }[] = [
    { kind: 'error', glyph: '✕', label: 'error', key: 'e' },
    { kind: 'prompt', glyph: '⊕', label: 'prompt', key: 'u' },
    { kind: 'subagent', glyph: '▸', label: 'subagent', key: 'b' },
    { kind: 'plan', glyph: '⊞', label: 'plan / question', key: 'p' },
  ];
  const shown = defs.filter((d) => lists[d.kind].length > 0);
  if (shown.length === 0) return null;
  return (
    <div className="conv-jump-cluster" role="group" aria-label="Jump to next landmark">
      {shown.map((d) => (
        <button
          key={d.kind}
          type="button"
          className="conv-jump-cluster-btn"
          data-jump-kind={d.kind}
          title={`Next ${d.label} (${d.key}) · shift-click for previous`}
          aria-label={`Next ${d.label}, ${lists[d.kind].length} total`}
          onClick={(ev) => jump(d.kind, ev.shiftKey ? -1 : 1, ev.currentTarget)}
        >
          <span className="conv-jump-cluster-glyph" aria-hidden="true">{d.glyph}</span>
          <span className="conv-jump-cluster-count">{lists[d.kind].length}</span>
        </button>
      ))}
    </div>
  );
}

// #177 S5 §3 — the outline sidebar + stats overview. Renders as a third grid
// column in `.conv-view` (a sibling of `.conv-rail`/`.conv-reader`, NOT nested
// inside the reader — Codex F13). Top: the "session at a glance" stats card.
// Below: the navigable landmark list from `deriveOutline`, independently
// scrollable. A click dispatches the existing deep-link jump
// (OPEN_CONVERSATION + jump), which handles loadUntil / scroll-center / flash /
// forced-open subagents. Scroll-sync: the reader's IntersectionObserver writes
// `convCurrentTurnUuid`; this panel highlights the matching top-level entry
// (aria-current) and keeps it scrolled into view.

// Per-entry leading glyph. Errors win, then plan/question, then per-type marks.
// Every glyph is aria-hidden (the label text carries the meaning); the tool
// fallback reuses the reader's generic box so a non-special turn is never blank.
function entryGlyph(e: OutlineEntry) {
  if (e.error) return <WarningIcon />;
  if (e.plan) return <PlanIcon />;
  if (e.question) return <QuestionIcon />;
  switch (e.type) {
    case 'human': return <ChatIcon />;
    case 'subagent': return <SubagentIcon />;
    case 'error': return <WarningIcon />;
    case 'meta': return <SystemIcon />;
    case 'assistant':
      return e.depth ? <ThinkingIcon /> : <ToolGenericIcon />;
    default: return <ToolGenericIcon />;
  }
}

// The stats card. `error_count === 0` hides the error row; the tool histogram
// shows the top-3 tools by count with a `+N more` suffix and the full list in a
// `title` tooltip. The error row is a button that jumps to the first error
// entry (the caller resolves that uuid).
function OutlineStatsCard({
  stats,
  onJumpFirstError,
}: {
  stats: OutlineStats;
  onJumpFirstError: (() => void) | null;
}) {
  const yours = stats.turns.human;
  const totalTokens =
    stats.tokens.input +
    stats.tokens.output +
    stats.tokens.cache_creation +
    stats.tokens.cache_read;

  // Tool histogram: top-3 by count (descending), then `+N more`. The full
  // sorted list goes into the title tooltip so nothing is lost at a glance.
  const toolPairs = useMemo(() => {
    return Object.entries(stats.tool_counts).sort((a, b) => b[1] - a[1]);
  }, [stats.tool_counts]);
  const topTools = toolPairs.slice(0, 3);
  const moreCount = toolPairs.length - topTools.length;
  const toolTitle = toolPairs.map(([name, n]) => `${name} ×${n}`).join('\n');

  // Model distribution: "claude-opus-4 ×12, claude-sonnet ×3".
  const modelPairs = useMemo(
    () => Object.entries(stats.models).sort((a, b) => b[1] - a[1]),
    [stats.models],
  );

  return (
    <div className="conv-outline-stats">
      <div className="conv-outline-stats-row conv-outline-stats-turns">
        <span className="conv-outline-stats-strong">{stats.turns.total}</span> turns
        {' · '}
        <span className="conv-outline-stats-strong">{yours}</span> yours
      </div>
      <div className="conv-outline-stats-row conv-outline-stats-meta">
        <span title="Session duration">{fmt.hhmm(stats.duration_seconds)}</span>
        {' · '}
        <span title="Total tokens">{fmt.tokens(totalTokens)} tok</span>
        {' · '}
        <span title="Session cost">{fmt.usd2(stats.cost_usd)}</span>
      </div>
      {stats.error_count > 0 && (
        <button
          type="button"
          className="conv-outline-stats-row conv-outline-stats-errors"
          onClick={() => onJumpFirstError?.()}
          disabled={!onJumpFirstError}
          title="Jump to the first error"
        >
          {stats.error_count} {stats.error_count === 1 ? 'error' : 'errors'}
        </button>
      )}
      {modelPairs.length > 0 && (
        <div className="conv-outline-stats-row conv-outline-stats-models">
          {modelPairs.map(([m, n]) => `${m} ×${n}`).join(', ')}
        </div>
      )}
      {topTools.length > 0 && (
        <div className="conv-outline-stats-row conv-outline-stats-tools" title={toolTitle}>
          {topTools.map(([name, n]) => `${name} ×${n}`).join('  ')}
          {moreCount > 0 ? `  +${moreCount} more` : ''}
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
  // #184 — deliberate full-panel re-render per `convCurrentTurnUuid` tick. The
  // reader keeps its own highlight imperative (a class toggle on the scrolled
  // element) precisely to avoid re-rendering its heavy MessageItems on every
  // scroll-sync tick; the panel makes the OPPOSITE trade and simply re-renders.
  // Its rows are trivial (a glyph + a label string, no Markdown / cards), so the
  // re-render cost is negligible, and a subscription-driven render is far simpler
  // than mirroring the reader's imperative aria-current bookkeeping here.
  const currentUuid = useSyncExternalStore(
    subscribeStore,
    () => getState().convCurrentTurnUuid,
  );
  const focusMode = useSyncExternalStore(subscribeStore, () => getState().convFocusMode);
  const reduced = useReducedMotion();

  const entries = useMemo(
    () => (outline ? deriveOutline(outline.turns, outline.subagent_meta) : []),
    [outline],
  );

  // uuid → OutlineTurn, so a jump can test the target's visibility under the
  // current focus mode before dispatching (spec §5).
  const turnByUuid = useMemo(() => {
    const m = new Map<string, OutlineTurn>();
    (outline?.turns ?? []).forEach((t) => m.set(t.uuid, t));
    return m;
  }, [outline]);

  // Reset to `all` IF the current focus mode would hide the target turn before
  // jumping (spec §5: never a silent jump behind a focus filter). The store
  // reducer no longer blanket-resets on same-session OPEN_CONVERSATION, so this
  // per-jump check is the authority for entry clicks + the stats error row.
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

  // First error entry's jump uuid (stats error row + future jump-to-next). Null
  // when the session has no error landmark, which disables the error row.
  const firstErrorUuid = useMemo(
    () => entries.find((e) => e.error || e.type === 'error')?.uuid ?? null,
    [entries],
  );

  // Auto-scroll the aria-current entry into view in the panel. Keyed on
  // currentUuid so it tracks the reader's topmost visible turn; reduced-motion
  // downgrades to an instant jump.
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
          <JumpCluster
            sessionId={sessionId}
            outline={outline}
            currentUuid={currentUuid}
            reduced={reduced}
            focusMode={focusMode}
          />
          <OutlineStatsCard
            stats={outline.stats}
            onJumpFirstError={firstErrorUuid != null ? () => jumpTo(firstErrorUuid) : null}
          />
          <ol className="conv-outline-list" ref={listRef}>
            {entries.map((e) => (
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
                  aria-current={
                    currentUuid != null && e.uuid === currentUuid && e.depth === 0
                      ? 'true'
                      : undefined
                  }
                  onClick={() => jumpTo(e.uuid)}
                  title={e.label}
                >
                  <span className="conv-outline-entry-glyph" aria-hidden="true">
                    {entryGlyph(e)}
                  </span>
                  <span className="conv-outline-entry-label">{e.label}</span>
                  {e.toolCount > 0 && (
                    <span className="conv-outline-entry-tools"> · {e.toolCount} tools</span>
                  )}
                </button>
              </li>
            ))}
          </ol>
        </>
      )}
    </nav>
  );
}
