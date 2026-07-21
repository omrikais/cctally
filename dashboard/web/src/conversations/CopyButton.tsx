import { useCallback, useRef, useState } from 'react';
import { useCopy } from './useCopy';
import { CopyIcon, CheckIcon } from './ConvIcons';
import { useAnonMode, useConversationRef } from './TranscriptContext';
import { fetchAnonPlan, scrubText } from './anonScrub';
import { conversationRefKey, sameConversationRef } from '../types/conversation';

// Compact, icon-only copy button (G2 §5b). The clipboard glyph swaps to a
// check while `copied`. aria-label carries the state (Copy → Copied) since the
// glyph is icon-only. onClick stops propagation so a copy click never toggles
// an enclosing <details>.
//
// #281 S4 — per-card copy follows the reader's Anonymize mode, FAIL-CLOSED:
// while the mode is ON the clipboard is written ONLY after the current session's
// anon-map has loaded AND applied successfully. On fetch failure, malformed wire
// data, or an invalid pattern the clipboard is left UNTOUCHED and the button
// shows an error state — never a silent raw copy while the UI says "anon". A
// session switch mid-flight discards the stale response (the awaited plan is
// re-checked against the session id captured at click time).
export function CopyButton({ text, className }: { text: string; className?: string }) {
  const { copied, copy } = useCopy();
  const anonMode = useAnonMode();
  const conversationRef = useConversationRef();
  const [errored, setErrored] = useState(false);
  const mountedRef = useRef(true);
  const conversationRefRef = useRef(conversationRef);
  conversationRefRef.current = conversationRef;

  const onClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      setErrored(false);
      // Mode OFF (or no session context) → today's raw copy, unchanged.
      if (!anonMode || !conversationRef) {
        copy(text);
        return;
      }
      const forSession = conversationRef;
      void (async () => {
        try {
          const plan = await fetchAnonPlan(forSession);
          // Session switched mid-flight → discard the stale plan, never write.
          if (!sameConversationRef(conversationRefRef.current, forSession)) return;
          const scrubbed = scrubText(text, plan); // may throw on a bad pattern
          copy(scrubbed);
        } catch {
          // Fail-closed: clipboard untouched, surface a visible error state.
          if (mountedRef.current) setErrored(true);
        }
      })();
    },
    [anonMode, conversationRef ? conversationRefKey(conversationRef) : null, text, copy],
  );

  // Track mount/unmount (stable ref callback: React calls it with the element on
  // mount and null on unmount) so the async catch never setStates on an
  // unmounted button.
  const setRef = useCallback((el: HTMLButtonElement | null) => {
    mountedRef.current = el !== null;
  }, []);

  const anonActive = anonMode && !!conversationRef;
  const label = errored
    ? 'Copy failed'
    : copied
      ? anonActive
        ? 'Copied (anonymized)'
        : 'Copied'
      : anonActive
        ? 'Copy (anonymized)'
        : 'Copy';

  return (
    <button
      ref={setRef}
      type="button"
      className={`conv-copy-btn ${anonActive ? 'conv-copy-btn-anon' : ''} ${errored ? 'conv-copy-btn-error' : ''} ${className ?? ''}`.trim()}
      aria-label={label}
      data-anon={anonActive ? '1' : undefined}
      onClick={onClick}
    >
      {errored ? '✕' : copied ? <CheckIcon /> : <CopyIcon />}
    </button>
  );
}
