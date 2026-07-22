import { useCallback, useEffect, useRef, useState } from 'react';
import { nextRovingIndex } from './menuKeyboard';
import { useOutsideDismiss } from './useOutsideDismiss';
import { ExportMenu } from './ExportMenu';
import { fmt } from '../lib/fmt';
import { conversationRefKey, type ConversationRefInput } from '../types/conversation';

// #228 S3 C2 — the mobile (≤640px) reader-header "⋯" overflow menu. Collapses
// the secondary reader actions — Export, Compare with…, Latest ↓, Expand-all,
// Collapse-all — off the always-visible header so reading starts in the top ~40%
// of a phone screen. Two read-only rows at the top surface the completion (✓ N)
// and cumulative-cost summaries that lose their inline chips on mobile.
//
// Built on the SAME menu primitive as ExportMenu / FocusMoreMenu
// (dashboard-gotchas): container-level Escape (onKeyDown on the role="menu" div,
// NOT per-item), focus captured at open + restored to the trigger on close,
// ≥44px touch targets (CSS), reduced-motion via CSS only. The action items are
// role="menuitem" (the Anonymize toggle is role="menuitemcheckbox" +
// aria-checked per the APG toggle-menu pattern, #304 S2 F5) with a single roving
// tabindex (only the active item is
// Tab-reachable); ArrowUp/Down wrap, Home/End jump, Escape closes. Index math is
// the pure `nextRovingIndex` helper; this component owns the imperative
// `.focus()`. Presentational — every action is a passed callback so the reader
// owns the dispatch and the unit tests stay pure (modal-level integration test).
//
// Export is the one self-contained popover among the actions, so it rides inside
// the menu as its own ExportMenu row (role="none") rather than a flat callback;
// the four bulk/jump/compare actions are flat menuitems.

export interface ReaderOverflowMenuProps {
  // Export rides as the embedded ExportMenu (its own nested popover).
  sessionId: ConversationRefInput;
  exportTitle?: string;
  // #281 S4 — the Anonymize toggle. The SAME store state as the desktop header
  // chip (the reader owns `anonMode` + `toggleAnonMode`), so a flip here updates
  // the chip and vice versa, and the embedded ExportMenu below honors it — a
  // desktop OFF no longer silently produces raw exports through the mobile menu.
  anonMode: boolean;
  onToggleAnon: () => void;
  // Compare with… — enters rail pick-mode anchored on this session.
  onCompare: () => void;
  // Latest ↓ — reset to the tail + jump/flash the final turn. Hidden when null
  // (a genuinely empty conversation, mirroring the desktop control's gate).
  onLatest: (() => void) | null;
  latestBusy?: boolean;
  // Bulk disclosure sweeps (the S2 ] / [ keys surfaced as menu actions).
  onExpandAll: () => void;
  onCollapseAll: () => void;
  // Read-only summary rows. `completionTotal` non-null → a "✓ N" row; cumulative
  // cost shows when `costTotal > 0` ("$through / $total", with a ~ when approx).
  completionTotal?: number | null;
  costCumulative?: number;
  costTotal?: number;
  costApprox?: boolean;
  // #304 S3 (Codex F3) — folding the desktop strip must not remove the ✓
  // Complete JUMP (the full strip's ✓ Complete is a scroll-to-completion
  // button). When the reader passes this callback AND `completionTotal` is set,
  // the read-only completion summary row becomes a real actionable menuitem
  // ("✓ Complete · N") that runs the jump then closes; the ≤1100 compact band
  // gains the repaired jump too. When unset, the read-only summary row renders
  // exactly as before. The cost row stays read-only.
  onCompletionJump?: (() => void) | null;
}

export function ReaderOverflowMenu({
  sessionId,
  exportTitle,
  anonMode,
  onToggleAnon,
  onCompare,
  onLatest,
  latestBusy = false,
  onExpandAll,
  onCollapseAll,
  completionTotal = null,
  costCumulative = 0,
  costTotal = 0,
  costApprox = false,
  onCompletionJump = null,
}: ReaderOverflowMenuProps) {
  const [open, setOpen] = useState(false);
  const restoreRef = useRef<Element | null>(null);
  // #238 R3 — pointerdown-outside dismiss. Silent (setOpen(false)), NOT the
  // focus-restoring close(): a pointerdown fires before the clicked-outside
  // control is focused, so close()'s trigger.focus() would yank focus back.
  const rootRef = useRef<HTMLDivElement>(null);
  useOutsideDismiss(rootRef, open, useCallback(() => setOpen(false), []));

  // The roving menuitems, in render order (Export rides separately as its own
  // popover row and is NOT in this roving set). The Anonymize toggle is the first
  // item — it sits directly under Export because it governs what Export/copy
  // produce. `null` entries are filtered out (e.g. Latest when the conversation
  // is empty), so the roving index always spans only the live items.
  const items = [
    { key: 'anon', kind: 'toggle', label: 'Anonymize', pressed: anonMode },
    { key: 'compare', kind: 'action', label: '⟷ Compare with…', run: onCompare },
    onLatest ? { key: 'latest', kind: 'action', label: `${latestBusy ? '… ' : ''}Latest ↓`, run: onLatest, busy: latestBusy } : null,
    { key: 'expand', kind: 'action', label: '⤢ Expand all', run: onExpandAll },
    { key: 'collapse', kind: 'action', label: '⤡ Collapse all', run: onCollapseAll },
    // #304 S3 (Codex F3) — the completion JUMP, appended LAST so the existing
    // roving indices above are undisturbed. Only present when the reader passes
    // the jump callback AND there is a completion total; otherwise completion
    // stays the read-only summary row below.
    onCompletionJump != null && completionTotal != null
      ? { key: 'complete', kind: 'action', label: `✓ Complete · ${completionTotal}`, run: onCompletionJump }
      : null,
  ].filter(Boolean) as (
    | { key: 'anon'; kind: 'toggle'; label: string; pressed: boolean }
    | { key: string; kind: 'action'; label: string; run: () => void; busy?: boolean }
  )[];
  const itemCount = items.length;
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const activeIndexRef = useRef(0);
  const setActive = useCallback((i: number) => {
    activeIndexRef.current = i;
    setActiveIndex(i);
  }, []);

  // The reader is not keyed by session, so an open menu would otherwise persist
  // across a session switch. Skip the initial effect: an unconditional initial
  // setOpen(false) can race with an immediate first click when passive effects
  // are delayed under load, closing the menu the user just opened.
  const sessionKey = conversationRefKey(sessionId);
  const previousSessionKeyRef = useRef(sessionKey);
  useEffect(() => {
    if (previousSessionKeyRef.current === sessionKey) return;
    previousSessionKeyRef.current = sessionKey;
    setOpen(false);
  }, [sessionKey]);

  const close = useCallback(() => {
    setOpen(false);
    const el = restoreRef.current;
    if (el instanceof HTMLElement) el.focus();
  }, []);

  const openAt = useCallback(
    (index: number) => {
      restoreRef.current = document.activeElement;
      setActive(index);
      setOpen(true);
    },
    [setActive],
  );

  const toggle = useCallback(() => {
    if (open) setOpen(false);
    else openAt(0);
  }, [open, openAt]);

  // On open, move focus to the active menuitem. Reads the index via ref so the
  // effect depends only on `open`.
  useEffect(() => {
    if (open) itemRefs.current[activeIndexRef.current]?.focus();
  }, [open]);

  const onMenuKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close();
        return;
      }
      const ni = nextRovingIndex(e.key, activeIndexRef.current, itemCount);
      if (ni !== null) {
        e.preventDefault();
        setActive(ni);
        itemRefs.current[ni]?.focus();
      }
    },
    [close, itemCount, setActive],
  );

  const onTriggerKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        openAt(0);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        openAt(itemCount - 1);
      }
    },
    [openAt, itemCount],
  );

  const pick = useCallback(
    (run: () => void) => {
      run();
      close();
    },
    [close],
  );

  const showCost = costTotal > 0;
  // #304 S3 (Codex F3) — the read-only completion summary row is suppressed when
  // the jump callback promotes completion to an actionable menuitem above.
  const showCompletion = completionTotal != null && onCompletionJump == null;

  return (
    <div
      ref={rootRef}
      className="conv-overflow"
      onBlur={(e) => {
        // Outside-click / focus-out close: if focus leaves the container, close.
        // The embedded ExportMenu lives inside this container, so opening it does
        // not blur us out (its own focus stays within currentTarget).
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      <button
        type="button"
        className="conv-overflow-toggle"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More actions"
        title="More actions"
        onClick={toggle}
        onKeyDown={onTriggerKeyDown}
      >
        ⋯
      </button>
      {open && (
        <div
          className="conv-overflow-menu"
          role="menu"
          aria-label="More actions"
          tabIndex={-1}
          onKeyDown={onMenuKeyDown}
        >
          {(showCompletion || showCost) && (
            <div className="conv-overflow-summary" role="none">
              {showCompletion && (
                <div className="conv-overflow-summary-row" role="none">
                  <span className="conv-overflow-summary-label">Completion</span>
                  <span className="conv-overflow-summary-value">✓ {completionTotal}</span>
                </div>
              )}
              {showCost && (
                <div className="conv-overflow-summary-row" role="none">
                  <span className="conv-overflow-summary-label">Cost</span>
                  <span className="conv-overflow-summary-value">
                    {costApprox ? '~' : ''}{fmt.usd2(costCumulative)} / {fmt.usd2(costTotal)}
                  </span>
                </div>
              )}
            </div>
          )}
          {/* Export is its own nested popover — rides as a row, not a menuitem.
              It honors the SAME anonMode as the toggle below (a desktop OFF now
              flows through to mobile exports). */}
          <div className="conv-overflow-export" role="none">
            <ExportMenu conversationRef={sessionId} title={exportTitle} anonMode={anonMode} />
          </div>
          {items.map((it, i) => (
            <button
              key={it.key}
              type="button"
              // #304 S2 (F5) — the Anonymize toggle uses APG toggle-menu semantics
              // (role=menuitemcheckbox + aria-checked); the flat actions stay
              // role=menuitem. Both share the role-agnostic roving tabindex set.
              role={it.kind === 'toggle' ? 'menuitemcheckbox' : 'menuitem'}
              tabIndex={i === activeIndex ? 0 : -1}
              ref={(el) => {
                itemRefs.current[i] = el;
              }}
              className={
                it.kind === 'toggle'
                  ? 'conv-overflow-item conv-overflow-item--toggle'
                  : 'conv-overflow-item'
              }
              aria-checked={it.kind === 'toggle' ? it.pressed : undefined}
              disabled={it.kind === 'action' ? it.busy : undefined}
              // The toggle flips in place (menu stays open — mirrors the desktop
              // chip); actions run then close the menu (focus-return).
              onClick={() => (it.kind === 'toggle' ? onToggleAnon() : pick(it.run))}
            >
              {it.kind === 'toggle' ? (
                <>
                  <span className="conv-overflow-item-label">🎭 {it.label}</span>
                  <span className="conv-overflow-item-state" aria-hidden="true">
                    {it.pressed ? 'On' : 'Off'}
                  </span>
                </>
              ) : (
                it.label
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
