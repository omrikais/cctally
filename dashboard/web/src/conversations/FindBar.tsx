import { useEffect, useRef, useState } from 'react';
import { dispatch } from '../store/store';
import { useConversationFind } from '../hooks/useConversationFind';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { loadFindRegex, saveFindRegex, loadFindCase, saveFindCase } from '../store/findPrefs';
import { normalizeConversationRef, type ConversationRefInput } from '../types/conversation';

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
  stepRef,
  tailRevision = 0,
}: {
  sessionId: ConversationRefInput;
  onClose: () => void;
  // (needle, caseSensitive, regex) — the reader builds the HighlightTerms value.
  // #223 supersedes S4 decision b: regex mode now reports its source for
  // best-effort inline highlighting (was forced to '' to suppress marks).
  onTermsChange: (needle: string, caseSensitive: boolean, regex: boolean) => void;
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
  const [cursor, setCursor] = useState(0);
  // Toggle state seeded from localStorage on mount, persisted on each flip.
  const [regex, setRegex] = useState(loadFindRegex);
  const [caseSensitive, setCaseSensitive] = useState(loadFindCase);
  const { anchors, total, truncated, mode, loading, error } = useConversationFind(
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
    onTermsChange(debouncedNeedle, caseSensitive, regex);
  }, [debouncedNeedle, regex, caseSensitive, onTermsChange]);

  // #217 S4 / I-1.6 — preserve the selected match BY UUID across a refresh
  // (Codex P2). `selectedUuidRef` holds the previously-selected uuid, written by
  // `step` when the user navigates (so it is ALREADY known before the next
  // `anchors` lands). On a new anchor list, re-find that uuid (findIndex -1 →
  // reset to 0) and write the resolved uuid back. This replaces the old "reset
  // cursor to 0 on any anchors change". Critically the ref is NOT recomputed in
  // an `anchors`-keyed effect (that would read the stale cursor against the new
  // list and lock onto the wrong match) — only `step` and this reconciliation
  // touch it.
  const selectedUuidRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = selectedUuidRef.current;
    const idx = prev ? anchors.findIndex((a) => a.uuid === prev) : -1;
    const next = idx >= 0 ? idx : 0;
    setCursor(next);
    selectedUuidRef.current = anchors.length ? (anchors[next]?.uuid ?? null) : null;
    // Keyed on the anchor LIST identity (a fresh fetch replaces it); the uuid
    // lookup keeps the cursor on the same match when it survived.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchors]);

  // Walk to a target index (wraps modulo length) and deep-link-jump there. The
  // dispatch is a SAME-session OPEN_CONVERSATION, so the store keeps find open;
  // expand_details opens the target turn's disclosures when the match was in a
  // tool/thinking block (the reader can't know which disclosure, so it opens
  // them all).
  const step = (delta: number) => {
    if (anchors.length === 0) return;
    const next = ((cursor + delta) % anchors.length + anchors.length) % anchors.length;
    setCursor(next);
    const a = anchors[next];
    selectedUuidRef.current = a.uuid;   // remember the selection for cursor-preservation
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef,
      jump: {
        ...(qualifiedInput ? { conversation_ref: conversationRef } : {}),
        session_id: conversationRef.key,
        uuid: a.uuid,
        expand_details: a.match_kinds.length > 0,
      },
    });
  };

  // Expose the live step closure to the reader's n/N bindings. Assigned every
  // render (step closes over the current cursor/anchors); cleared on unmount.
  if (stepRef) stepRef.current = step;
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
    if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); step(e.shiftKey ? -1 : 1); }
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

  const has = anchors.length > 0;
  const current = has ? anchors[cursor] : null;
  const counter = `${has ? cursor + 1 : 0} / ${total}`;
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
        onClick={() => step(-1)}
      >‹</button>
      <button
        type="button"
        className="conv-findbar-nav"
        aria-label="Next match"
        title="Next match (Enter)"
        disabled={!has}
        onClick={() => step(1)}
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
