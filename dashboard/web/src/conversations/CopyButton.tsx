import { useCallback, useRef, useState, useSyncExternalStore } from 'react';
import { useCopy } from './useCopy';
import { CopyIcon, CheckIcon } from './ConvIcons';
import { useAnonMode, useConversationRef } from './TranscriptContext';
import {
  ANON_UNAVAILABLE_STATUS,
  AnonRequestError,
  anonUnavailableMessage,
  fetchAnonPlan,
  scrubText,
} from './anonScrub';
import { dispatch, selectConvAnonRefusal, subscribeStore } from '../store/store';
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
  // #850 §4.9 — the last server decision this page observed for the SELECTED
  // conversation, as disclosure only. The fail-closed guarantee is the request
  // below, which asks the server every time and never trusts this record.
  const refusal = useSyncExternalStore(subscribeStore, () => selectConvAnonRefusal());

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
          // The record is keyed by the ref captured at click time, so a late
          // 2xx clears THAT conversation's record and no other's.
          dispatch({
            type: 'SET_CONV_ANON_REFUSAL',
            conversationRef: forSession,
            refused: false,
          });
          // Session switched mid-flight → discard the stale plan, never write.
          if (!sameConversationRef(conversationRefRef.current, forSession)) return;
          const scrubbed = scrubText(text, plan); // may throw on a bad pattern
          copy(scrubbed);
        } catch (err) {
          // Fail-closed: clipboard untouched. A typed 409 arms this
          // conversation's refusal record, which is the M5 disclosure; any
          // other failure keeps today's error state.
          if (err instanceof AnonRequestError && err.status === ANON_UNAVAILABLE_STATUS) {
            dispatch({
              type: 'SET_CONV_ANON_REFUSAL',
              conversationRef: forSession,
              refused: true,
              message: anonUnavailableMessage(err),
            });
            return;
          }
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
  // The refusal is disclosure for the ANONYMIZED action only; a raw copy is
  // unaffected and keeps its own label. The button stays activatable, so an
  // activation re-requests and a 2xx clears the record in place.
  const anonRefused = anonActive ? refusal : null;
  const label = anonRefused
    ? anonRefused
    : errored
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
      className={`conv-copy-btn ${anonActive ? 'conv-copy-btn-anon' : ''} ${errored ? 'conv-copy-btn-error' : ''} ${anonRefused ? 'conv-copy-btn-anon-unavailable' : ''} ${className ?? ''}`.trim()}
      aria-label={label}
      title={anonRefused ?? undefined}
      data-anon={anonActive ? '1' : undefined}
      data-anon-unavailable={anonRefused ? '1' : undefined}
      onClick={onClick}
    >
      {anonRefused || errored ? '✕' : copied ? <CheckIcon /> : <CopyIcon />}
    </button>
  );
}
