import { useEffect, useRef, useState } from 'react';
import { dispatch } from '../store/store';
import { useConversationFind } from '../hooks/useConversationFind';
import { useDebouncedValue } from '../hooks/useDebouncedValue';

// #177 S6 — the floating in-conversation find bar (Cmd+F style pill, top-right
// inside the reader column). Owns its needle + a 1-based match cursor, drives
// useConversationFind, and walks the returned rendered-turn anchors via
// OPEN_CONVERSATION jumps (same-session, so the reader pages-to + scrolls + the
// store leaves find open). `onTermsChange` reports the DEBOUNCED needle up so
// the reader can feed prose <mark> highlighting (memoized — only re-renders on
// debounced change). `onClose` is the reader's focus-restore callback (returns
// keyboard focus to the thread so j/k resume).
//
// Input keys: Enter = next, Shift+Enter = prev, Esc = close. The bar also
// registers n/N at the reader level while open (the input is blurred case).
export function FindBar({
  sessionId,
  onClose,
  onTermsChange,
  stepRef,
}: {
  sessionId: string;
  onClose: () => void;
  onTermsChange: (terms: string) => void;
  // The reader holds this so its n/N bindings (active while the bar is open +
  // the input is blurred) can step the same cursor. Assigned to the live `step`
  // closure each render; null when no bar is mounted.
  stepRef?: React.MutableRefObject<((delta: number) => void) | null>;
}) {
  const [needle, setNeedle] = useState('');
  const [cursor, setCursor] = useState(0);
  const { anchors, total, truncated, mode, loading, error } = useConversationFind(sessionId, needle);
  const inputRef = useRef<HTMLInputElement>(null);

  // Auto-focus on mount (the bar mounts on open).
  useEffect(() => { inputRef.current?.focus(); }, []);

  // Report the debounced needle up for the prose-mark context (mirrors the
  // hook's own 200ms debounce so marks land in lockstep with the anchor list).
  const debouncedNeedle = useDebouncedValue(needle.trim(), 200, '');
  useEffect(() => { onTermsChange(debouncedNeedle); }, [debouncedNeedle, onTermsChange]);

  // Reset the cursor whenever the anchor LIST identity changes (a fresh fetch
  // replaces it) so a new needle starts at the first match. #177 S6 M7 — this
  // keys on array IDENTITY, so EVERY refetch resets the cursor even when the
  // results are byte-identical. That's intentional per spec ("re-running the
  // query refreshes"): a refetch is a fresh query, so it restarts at match 1.
  useEffect(() => { setCursor(0); }, [anchors]);

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
    dispatch({
      type: 'OPEN_CONVERSATION',
      sessionId,
      jump: { session_id: sessionId, uuid: a.uuid, expand_details: a.match_kinds.length > 0 },
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

  const onKeyDown = (e: React.KeyboardEvent) => {
    // Enter / Escape are NAMED keys, so the global keydown dispatcher does NOT
    // swallow them while the input is focused (only length-1 keys are). Without
    // stopPropagation, the ConversationsView global Escape would ALSO fire and
    // exit the workspace, and a global Enter binding could double-handle. The
    // input owns these keys, so stop them from reaching the document listener.
    if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); step(e.shiftKey ? -1 : 1); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
  };

  const has = anchors.length > 0;
  const current = has ? anchors[cursor] : null;
  const counter = `${has ? cursor + 1 : 0} / ${total}`;

  return (
    <div className="conv-findbar" role="search" aria-label="Find within this conversation">
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
      <span className="conv-findbar-count" aria-live="polite">
        {counter}
        {truncated && <span className="conv-findbar-note"> · first 500</span>}
      </span>
      {current && current.match_kinds.length > 0 && (
        <span className="conv-findbar-kind">{current.match_kinds.join(' ')}</span>
      )}
      {mode === 'like' && <span className="conv-findbar-hint">basic search</span>}
      {/* #177 S6 M4 — surface a real fetch failure so `0 / 0` isn't mistaken for
          "zero matches". Reuses the `basic search` hint styling. */}
      {error && <span className="conv-findbar-hint">find failed</span>}
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
