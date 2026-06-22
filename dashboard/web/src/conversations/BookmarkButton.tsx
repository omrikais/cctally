import { useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { BookmarkIcon, BookmarkFilledIcon } from './ConvIcons';

// #217 S6 F4 — per-turn ★ bookmark toggle + optional note. Reads bookmarked/note
// via PRIMITIVE store selectors (boolean / string) so useSyncExternalStore stays
// stable and the enclosing memoized MessageItem doesn't re-subscribe. The note
// editor sets inputMode='note' so reader hotkeys are suppressed while typing
// (the reader's guard gates on inputMode === null); its keydown stopPropagation's
// Esc/Enter so they don't bubble to the reader.
export function BookmarkButton({ sessionId, uuid, className }: { sessionId: string; uuid: string; className?: string }) {
  void sessionId; // mutations resolve the session from the store; prop kept for symmetry with PermalinkButton.
  const bookmarked = useSyncExternalStore(subscribeStore, () => uuid in getState().convBookmarks);
  const note = useSyncExternalStore(subscribeStore, () => getState().convBookmarks[uuid]?.note ?? '');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');

  const openEditor = () => { setDraft(note); setEditing(true); dispatch({ type: 'SET_INPUT_MODE', mode: 'note' }); };
  const closeEditor = (save: boolean) => {
    if (save) dispatch({ type: 'SET_BOOKMARK_NOTE', uuid, note: draft.trim() });
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
        onClick={(e) => { e.stopPropagation(); dispatch({ type: 'TOGGLE_BOOKMARK', uuid }); }}
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
            else if (e.key === 'Escape') closeEditor(false);
          }}
          onBlur={() => closeEditor(true)}
        />
      )}
    </span>
  );
}
