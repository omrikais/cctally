import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { useKeymap } from '../hooks/useKeymap';
import { useIsMobile } from '../hooks/useIsMobile';
import { useConversationOutline } from '../hooks/useConversationOutline';
import { transcriptsEnabled } from '../lib/transcripts';
import { ConversationRail } from './ConversationRail';
import { ConversationReader } from './ConversationReader';
import { OutlinePanel } from './OutlinePanel';
import { ChatIcon } from './ConvIcons';

// Two-pane Conversations workspace (spec §4). Mounted by App.tsx only
// when view==='conversations', so its keymap bindings exist only while
// active (no collision with the unmounted dashboard panels). Registers
// view-aware '/' (focus rail search) and Esc (clear search, else exit).
export function ConversationsView() {
  const selected = useSyncExternalStore(subscribeStore, () => getState().selectedConversationId);
  const outlineOpen = useSyncExternalStore(subscribeStore, () => getState().convOutlineOpen);
  const env = useSnapshot();
  const isMobile = useIsMobile();
  // #177 S5 — full-session outline + stats for the selected conversation. The
  // hook owns its own SSE-tick revalidation; a null `selected` yields a null
  // outline. Shared with the reader (toggle button + scroll-sync registration in
  // Tasks 4/5) and the OutlinePanel (third grid column / mobile slide-over).
  const { outline } = useConversationOutline(selected);

  useKeymap(CONVERSATIONS_BINDINGS);

  if (!transcriptsEnabled(env)) {
    return (
      <div className="conv-disabled">
        Transcript viewing is disabled. Enable it with
        {' '}<code>cctally config set dashboard.expose_transcripts true</code>{' '}
        (loopback is always allowed; restart the dashboard to apply).
      </div>
    );
  }

  // Mobile: rail until a conversation is chosen, then reader (+ back). The
  // outline rides as a slide-over SHEET (not a column) gated on the same store
  // flag, opened via the reader-head toggle; a backdrop button dispatches
  // TOGGLE to dismiss it (#177 S5 §3).
  if (isMobile) {
    return (
      <div className="conv-view conv-view--mobile">
        {selected == null
          ? <ConversationRail />
          : <>
              <ConversationReader sessionId={selected} outline={outline} mobileBack />
              {outlineOpen && (
                <>
                  <button
                    type="button"
                    className="conv-outline-backdrop"
                    aria-label="Close outline"
                    onClick={() => dispatch({ type: 'TOGGLE_CONV_OUTLINE' })}
                  />
                  <div className="conv-outline-sheet">
                    <OutlinePanel sessionId={selected} outline={outline} />
                  </div>
                </>
              )}
            </>}
      </div>
    );
  }
  return (
    <div className={['conv-view', outlineOpen && selected != null ? 'conv-view--outline' : ''].filter(Boolean).join(' ')}>
      <ConversationRail />
      {selected != null
        ? <ConversationReader sessionId={selected} outline={outline} />
        : <div className="conv-reader conv-reader--empty">
            <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true"><ChatIcon /></span>
              <div className="conv-state-title">Select a conversation</div>
              <div className="conv-state-hint">Choose one from the list to start reading.</div></div>
          </div>}
      {outlineOpen && selected != null && (
        <OutlinePanel sessionId={selected} outline={outline} />
      )}
    </div>
  );
}

// Module-scoped stable identity (useKeymap re-registers on array identity
// change). View gating (#156) is declared via `view:'conversations'` and
// enforced by the dispatcher; `when` carries only the transient guards.
const inView = () => !getState().openModal && getState().inputMode === null;
const CONVERSATIONS_BINDINGS = [
  {
    key: '/', scope: 'global' as const, view: 'conversations' as const, when: inView,
    action: () => {
      const el = document.querySelector<HTMLInputElement>('.conv-rail-search input');
      el?.focus(); el?.select();
    },
  },
  {
    key: 'Escape', scope: 'global' as const, view: 'conversations' as const,
    when: () => !getState().openModal,
    action: () => {
      if (getState().conversationSearch) { dispatch({ type: 'SET_CONVERSATION_SEARCH', text: '' }); return; }
      dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    },
  },
];
