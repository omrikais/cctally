import { useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { BookmarkIcon, BookmarkFilledIcon } from './ConvIcons';
import { useConversationRef } from './TranscriptContext';
import { legacyClaudeConversationRef } from '../types/conversation';

// #217 S6 F4 — per-turn ★ bookmark toggle + optional note. Reads bookmarked/note
// via PRIMITIVE store selectors (boolean / string) so useSyncExternalStore stays
// stable and the enclosing memoized MessageItem doesn't re-subscribe. The note
// editor sets inputMode='note' so reader hotkeys are suppressed while typing
// (the reader's guard gates on inputMode === null); its keydown stopPropagation's
// Esc/Enter so they don't bubble to the reader.
export function BookmarkButton({ sessionId, uuid, className }: { sessionId: string; uuid: string; className?: string }) {
  const conversationRef = useConversationRef() ?? legacyClaudeConversationRef(sessionId);
  // #217 S6 F4 (review) — the sessionId prop is now load-bearing: it's threaded
  // into both mutations so a bookmark always targets THIS button's session, even
  // if a future caller renders the button outside the selected-conversation path
  // (the reducers fall back to state.selectedConversationId when it's absent).
  const bookmarked = useSyncExternalStore(subscribeStore, () => uuid in getState().convBookmarks);
  const note = useSyncExternalStore(subscribeStore, () => getState().convBookmarks[uuid]?.note ?? '');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  // Escape→discard fights the unmount blur. When Escape sets editing=false the
  // <input> unmounts and the browser fires a native blur → onBlur=closeEditor(true)
  // would SAVE the discarded draft. cancelledRef latches the discard so the
  // trailing blur is swallowed instead of overwriting the discard.
  const cancelledRef = useRef(false);

  const openEditor = () => { setDraft(note); setEditing(true); dispatch({ type: 'SET_INPUT_MODE', mode: 'note' }); };
  const closeEditor = (save: boolean) => {
    if (save) dispatch({ type: 'SET_BOOKMARK_NOTE', uuid, conversationRef, note: draft.trim() });
    setEditing(false);
    dispatch({ type: 'SET_INPUT_MODE', mode: null });
  };

  return (
    <span className={`conv-bookmark ${className ?? ''}`.trim()}>
      <button
        type="button"
        className="conv-copy-btn conv-bookmark-btn"
        aria-pressed={bookmarked}
        aria-label={bookmarked ? 'Remove bookmark' : 'Bookmark this turn'}
        onClick={(e) => { e.stopPropagation(); dispatch({ type: 'TOGGLE_BOOKMARK', uuid, conversationRef }); }}
      >
        {bookmarked ? <BookmarkFilledIcon /> : <BookmarkIcon />}
      </button>
      {bookmarked && !editing && (
        <button type="button" className="conv-bookmark-note-toggle" onClick={(e) => { e.stopPropagation(); openEditor(); }}>
          {note ? note : 'note…'}
        </button>
      )}
      {bookmarked && editing && (
        <input
          className="conv-bookmark-note-input"
          aria-label="Bookmark note"
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === 'Enter') closeEditor(true);
            else if (e.key === 'Escape') { cancelledRef.current = true; closeEditor(false); }
          }}
          onBlur={() => {
            // Swallow the blur that the Escape unmount triggers — it must not
            // resurrect the discarded draft as a save (the editor is already closed).
            if (cancelledRef.current) { cancelledRef.current = false; return; }
            closeEditor(true);
          }}
        />
      )}
    </span>
  );
}
