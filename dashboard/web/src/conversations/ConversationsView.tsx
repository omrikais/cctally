import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { useKeymap } from '../hooks/useKeymap';
import { useIsMobile } from '../hooks/useIsMobile';
import { transcriptsEnabled } from '../lib/transcripts';
import { ConversationRail } from './ConversationRail';
import { ConversationReader } from './ConversationReader';

// Two-pane Conversations workspace (spec §4). Mounted by App.tsx only
// when view==='conversations', so its keymap bindings exist only while
// active (no collision with the unmounted dashboard panels). Registers
// view-aware '/' (focus rail search) and Esc (clear search, else exit).
export function ConversationsView() {
  const selected = useSyncExternalStore(subscribeStore, () => getState().selectedConversationId);
  const env = useSnapshot();
  const isMobile = useIsMobile();

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

  // Mobile: rail until a conversation is chosen, then reader (+ back).
  if (isMobile) {
    return (
      <div className="conv-view conv-view--mobile">
        {selected == null
          ? <ConversationRail />
          : <ConversationReader sessionId={selected} mobileBack />}
      </div>
    );
  }
  return (
    <div className="conv-view">
      <ConversationRail />
      {selected != null
        ? <ConversationReader sessionId={selected} />
        : <div className="conv-reader conv-reader--empty">
            <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true">💬</span>
              <div className="conv-state-title">Select a conversation</div>
              <div className="conv-state-hint">Choose one from the list to start reading.</div></div>
          </div>}
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
