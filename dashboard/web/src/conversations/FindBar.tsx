import { useEffect, useRef, useState } from 'react';
import { dispatch } from '../store/store';
import { useConversationFind } from '../hooks/useConversationFind';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { loadFindRegex, saveFindRegex, loadFindCase, saveFindCase } from '../store/findPrefs';
import {
  buildConversationJump,
  normalizeConversationRef,
  type ConversationRefInput,
  type FindOccurrence,
} from '../types/conversation';
import type { FindTarget } from '../hooks/useConversationFind';
import type { ExactFindState } from './HighlightContext';

// #177 S6 — the floating in-conversation find bar (Cmd+F style pill, top-right
// inside the reader column). Owns its needle + a 1-based match cursor, drives
// useConversationFind, and walks the returned rendered-turn anchors via
// OPEN_CONVERSATION jumps (same-session, so the reader pages-to + scrolls + the
// store leaves find open). `onTermsChange` reports the DEBOUNCED needle + the
// case + regex flags up so the reader can feed prose <mark> highlighting
// (case-aware; #223 — regex mode now reports its source for best-effort inline
// highlighting too, superseding S4 decision b). `onClose` is the reader's
// focus-restore callback.
//
// #217 S4 / I-1 power features: `.*` regex + `Aa` case toggles (persisted via
// findPrefs), a focus trap (Tab/Shift+Tab cycle within the bar; Esc closes), an
// invalid-regex alert, and live-refresh on the reader's monotonic `tailRevision`
// with the selected match preserved BY UUID across the refresh.
//
// Input keys: Enter = next, Shift+Enter = prev, Esc = close. The bar also
// registers n/N at the reader level while open (the input is blurred case).
export function FindBar({
  sessionId,
  onClose,
  onTermsChange,
  onExactFindChange,
  stepRef,
  tailRevision = 0,
}: {
  sessionId: ConversationRefInput;
  onClose: () => void;
  // (needle, caseSensitive, regex) — the reader builds the HighlightTerms value.
  // #223 supersedes S4 decision b: regex mode now reports its source for
  // best-effort inline highlighting (was forced to '' to suppress marks).
  onTermsChange: (needle: string, caseSensitive: boolean, regex: boolean) => void;
  onExactFindChange?: (state: ExactFindState | null) => void;
  // The reader holds this so its n/N bindings (active while the bar is open +
  // the input is blurred) can step the same cursor. Assigned to the live `step`
  // closure each render; null when no bar is mounted.
  stepRef?: React.MutableRefObject<((delta: number) => void) | null>;
  // #217 S4 / I-1.6 — the reader's monotonic live-tail merge counter; a bump
  // re-runs the find query (debounced) against the grown corpus.
  tailRevision?: number;
}) {
  const qualifiedInput = typeof sessionId !== 'string';
  const conversationRef = normalizeConversationRef(sessionId);
  const [needle, setNeedle] = useState('');
  // Toggle state seeded from localStorage on mount, persisted on each flip.
  const [regex, setRegex] = useState(loadFindRegex);
  const [caseSensitive, setCaseSensitive] = useState(loadFindCase);
  const {
    selected,
    occurrences,
    selectedIndex,
    total,
    truncated,
    semantics,
    status,
    selectionStale,
    mode,
    loading,
    error,
    step,
  } = useConversationFind(
    conversationRef, needle, { regex, case: caseSensitive, tailRevision });
  const inputRef = useRef<HTMLInputElement>(null);
  const barRef = useRef<HTMLDivElement>(null);

  // Auto-focus on mount (the bar mounts on open).
  useEffect(() => { inputRef.current?.focus(); }, []);

  // Report the debounced needle + case + regex flags up for the prose-mark
  // context (mirrors the hook's own 200ms debounce so marks land in lockstep).
  // #223 supersedes S4 decision b: regex mode now reports its source so the
  // reader can drive best-effort inline highlighting (was forced to '').
  const debouncedNeedle = useDebouncedValue(needle.trim(), 200, '');
  useEffect(() => {
    onTermsChange(semantics === 'occurrence' ? '' : debouncedNeedle, caseSensitive, regex);
  }, [debouncedNeedle, regex, caseSensitive, semantics, onTermsChange]);

  useEffect(() => {
    if (!onExactFindChange) return;
    if (semantics === 'occurrence') {
      onExactFindChange({
        occurrences,
        selectedOccurrenceId: selected && 'occurrence_id' in selected
          ? selected.occurrence_id
          : null,
      });
    } else {
      onExactFindChange(null);
    }
    return () => onExactFindChange(null);
  }, [semantics, occurrences, selected, onExactFindChange]);

  const dispatchTarget = (a: FindTarget | null) => {
    if (!a) return;
    const occurrence = 'occurrence_id' in a ? a as FindOccurrence : null;
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef,
      // Round 4 — through the shared builder like every other jump site. The
      // find bar is the only caller that supplies `expandDetails`, and it has no
      // inner anchor, which round 7's options object lets it express by simply
      // not naming `innerAnchorKey`.
      //
      // #463 S4 merge — occurrence-exact find arrived on `main` writing its own
      // literal here, which is the exact construction `jumpConstruction.test.ts`
      // forbids. Its two additions are preserved verbatim in behavior: an
      // occurrence decides `expand_details` from its own disclosure list rather
      // than from the anchor's match kinds, and the occurrence itself rides on
      // the jump. Both now go through the builder.
      jump: buildConversationJump(conversationRef, a.uuid, qualifiedInput, {
        expandDetails: occurrence ? occurrence.disclosure.length > 0 : a.match_kinds.length > 0,
        findOccurrence: occurrence,
      }),
    });
  };

  const navigate = (delta: number) => {
    const target = step(delta);
    if (target instanceof Promise) void target.then(dispatchTarget);
    else dispatchTarget(target);
  };

  // Exact results select and land their first occurrence as soon as the server
  // page becomes ready. Legacy section semantics retain their historical
  // explicit-step behavior.
  const autoLandedRef = useRef<string | null>(null);
  useEffect(() => {
    if (semantics !== 'occurrence' || !selected || !('occurrence_id' in selected)) return;
    if (autoLandedRef.current === selected.occurrence_id) return;
    autoLandedRef.current = selected.occurrence_id;
    dispatchTarget(selected);
    // dispatchTarget is intentionally render-local: its inputs are represented
    // by the selected occurrence and normalized conversation identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [semantics, selected, conversationRef.key]);

  // Expose the live step closure to the reader's n/N bindings. Assigned every
  // render (step closes over the current cursor/anchors); cleared on unmount.
  if (stepRef) stepRef.current = navigate;
  useEffect(() => () => { if (stepRef) stepRef.current = null; }, [stepRef]);

  const close = () => {
    dispatch({ type: 'CLOSE_CONV_FIND' });
    onClose();
  };

  const toggleRegex = () => setRegex((r) => { const v = !r; saveFindRegex(v); return v; });
  const toggleCase = () => setCaseSensitive((c) => { const v = !c; saveFindCase(v); return v; });

  const onKeyDown = (e: React.KeyboardEvent) => {
    // Enter is a NAMED key, so the global keydown dispatcher does NOT swallow it
    // while the input is focused (only length-1 keys are). Without
    // stopPropagation a global Enter binding could double-handle, so the input
    // owns Enter/Shift+Enter (next/prev) and stops it from reaching the document
    // listener. Escape is handled at the bar-container level (`onBarKeyDown`),
    // not here, so it behaves identically from the input AND from any bar button
    // — see that handler's note.
    if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); navigate(e.shiftKey ? -1 : 1); }
  };

  // #217 S4 / I-1.4 — focus trap + the bar-level Escape close. Both are handled
  // at the bar CONTAINER (not on the input) so a key pressed while focus is on
  // ANY control — the input OR a button (Close, regex/case toggles, prev/next)
  // — behaves the same. React events bubble through the React tree, so an Escape
  // on a bar button reaches this container handler. Without owning Escape here,
  // an Escape on a focused button would bubble PAST the bar to the document
  // keydown listener, firing the ConversationsView global Escape and tearing
  // down the whole reader (URL → '/') — the #217 S4 QA bug. So:
  //   - Tab/Shift+Tab cycle within the bar's tabbable controls (focus trap),
  //     computed from the live control set so it adapts to disabled nav buttons.
  //   - Escape closes ONLY the find bar (CLOSE_CONV_FIND + restore thread focus
  //     via onClose) and stopPropagation() keeps it from reaching the document
  //     listener, regardless of which control held focus.
  const onBarKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key !== 'Tab') return;
    const bar = barRef.current;
    if (!bar) return;
    const focusables = Array.from(
      bar.querySelectorAll<HTMLElement>('input, button'),
    ).filter((el) => !(el as HTMLButtonElement).disabled);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
  };

  const has = selected != null;
  const current = selected;
  const counter = status === 'indexing'
    ? 'indexing exact matches'
    : semantics === 'occurrence'
      ? `${has ? selectedIndex + 1 : 0} / ${total} matches`
      : `${has ? selectedIndex + 1 : 0} / ${total} containing sections`;
  // #228 S4 D8 — an always-visible mode tag spelling out the active toggles
  // (regex / case / regex · case). It survives typing (unlike a placeholder cue)
  // and answers "what does `.*`/`Aa` mean?" Pure render from the existing
  // regex/caseSensitive state — no new persistence, no new data.
  const modeLabel = [regex && 'regex', caseSensitive && 'case'].filter(Boolean).join(' · ');

  return (
    <div
      className="conv-findbar"
      role="search"
      aria-label="Find within this conversation"
      ref={barRef}
      onKeyDown={onBarKeyDown}
    >
      <input
        ref={inputRef}
        className="conv-findbar-input"
        type="text"
        aria-label="Find in conversation"
        placeholder="Find…"
        value={needle}
        onChange={(e) => setNeedle(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <button
        type="button"
        className="conv-findbar-toggle"
        aria-pressed={regex}
        aria-label="Regular expression"
        title="Regular expression (.*)"
        onClick={toggleRegex}
      >.*</button>
      <button
        type="button"
        className="conv-findbar-toggle"
        aria-pressed={caseSensitive}
        aria-label="Case-sensitive"
        title="Case-sensitive (Aa)"
        onClick={toggleCase}
      >Aa</button>
      {modeLabel && (
        <span className="conv-findbar-mode" aria-label={`search mode: ${modeLabel}`}>{modeLabel}</span>
      )}
      <span className="conv-findbar-count" aria-live="polite">
        {counter}
        {truncated && <span className="conv-findbar-note"> · first 500</span>}
        {selectionStale && <span className="conv-findbar-note"> · previous match changed</span>}
      </span>
      {current && current.match_kinds.length > 0 && (
        <span className="conv-findbar-kind">{current.match_kinds.join(' ')}</span>
      )}
      {mode === 'like' && !error && <span className="conv-findbar-hint">basic search</span>}
      {/* #217 S4 — an invalid-regex 400 surfaces as a role="alert" hint
          (announced); reuses the hint styling. The hook already maps every
          failure to its user-facing wording ('invalid regex' / 'find failed'),
          so render it verbatim rather than re-deriving — otherwise the hook's
          generic string would be dead. */}
      {error && (
        <span className="conv-findbar-hint" role="alert">{error}</span>
      )}
      {loading && <span className="conv-findbar-spin" aria-hidden="true" />}
      <button
        type="button"
        className="conv-findbar-nav"
        aria-label="Previous match"
        title="Previous match (Shift+Enter)"
        disabled={!has}
        onClick={() => navigate(-1)}
      >‹</button>
      <button
        type="button"
        className="conv-findbar-nav"
        aria-label="Next match"
        title="Next match (Enter)"
        disabled={!has}
        onClick={() => navigate(1)}
      >›</button>
      <button
        type="button"
        className="conv-findbar-close"
        aria-label="Close find"
        title="Close (Esc)"
        onClick={close}
      >✕</button>
    </div>
  );
}
