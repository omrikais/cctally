import { useCopy } from './useCopy';
import { LinkIcon, CheckIcon } from './ConvIcons';
import { permalinkUrl, reflectTurnUrl } from '../store/urlRouting';
import { useConversationRef } from './TranscriptContext';
import { legacyClaudeConversationRef } from '../types/conversation';

// Per-turn permalink (#169). Mirrors CopyButton: icon-only, useCopy's 1100ms
// check-swap + clipboard fallback, stopPropagation so a click never toggles an
// enclosing <details>. On click it copies the absolute deep-link AND reflects
// the address bar to this turn (replaceState) — but does NOT dispatch a jump,
// so the turn already under the cursor is not re-scrolled/flashed.
export function PermalinkButton({ sessionId, uuid, className }: { sessionId: string; uuid: string; className?: string }) {
  const conversationRef = useConversationRef() ?? legacyClaudeConversationRef(sessionId);
  const { copied, copy } = useCopy();
  return (
    <button
      type="button"
      className={`conv-copy-btn ${className ?? ''}`.trim()}
      aria-label={copied ? 'Link copied' : 'Copy link to this turn'}
      onClick={(e) => {
        e.stopPropagation();
        copy(permalinkUrl(window.location.origin, window.location.pathname, conversationRef, uuid));
        reflectTurnUrl(conversationRef, uuid);
      }}
    >
      {copied ? <CheckIcon /> : <LinkIcon />}
    </button>
  );
}
