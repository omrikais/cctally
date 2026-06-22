import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { useKeymap } from '../hooks/useKeymap';
import { useIsMobile } from '../hooks/useIsMobile';
import { useConversationOutline } from '../hooks/useConversationOutline';
import { useFindHotkey } from '../hooks/useFindHotkey';
import { transcriptsEnabled } from '../lib/transcripts';
import { ConversationRail } from './ConversationRail';
import { ConversationReader } from './ConversationReader';
import { ComparisonView } from './ComparisonView';
import { OutlinePanel } from './OutlinePanel';
import { OutlineResizer } from '../components/OutlineResizer';
import { ChatIcon } from './ConvIcons';

// Two-pane Conversations workspace (spec §4). Mounted by App.tsx only
// when view==='conversations', so its keymap bindings exist only while
// active (no collision with the unmounted dashboard panels). Registers
// view-aware '/' (focus rail search) and Esc (clear search, else exit).
export function ConversationsView() {
  const selected = useSyncExternalStore(subscribeStore, () => getState().selectedConversationId);
  // #217 S7 F10 — when a comparison is open, the whole workspace renders the
  // side-by-side ComparisonView instead of the single reader (see the branch
  // below). Subscribed here so an OPEN_COMPARE / SWAP_COMPARE / CLOSE_COMPARE
  // re-renders the view.
  const compare = useSyncExternalStore(subscribeStore, () => getState().compare);
  const outlineOpen = useSyncExternalStore(subscribeStore, () => getState().convOutlineOpen);
  // #217 S3 E6(b) — the persisted outline column width, driven into the grid as a
  // CSS custom property on the desktop shell only.
  const outlineWidth = useSyncExternalStore(subscribeStore, () => getState().convOutlineWidth);
  // #205 S1 — the ephemeral mobile outline-sheet flag (default closed, not
  // persisted). The mobile slide-over gates on this so it never auto-buries the
  // transcript on open; the desktop column below keeps gating on convOutlineOpen.
  const outlineMobileOpen = useSyncExternalStore(subscribeStore, () => getState().convOutlineMobileOpen);
  const env = useSnapshot();
  const isMobile = useIsMobile();
  // #177 S5 — full-session outline + stats for the selected conversation. The
  // hook owns its own SSE-tick revalidation; a null `selected` yields a null
  // outline. Shared with the reader (toggle button + scroll-sync registration in
  // Tasks 4/5) and the OutlinePanel (third grid column / mobile slide-over).
  const { outline } = useConversationOutline(selected);

  useKeymap(CONVERSATIONS_BINDINGS);
  // #217 S4 / I-1.5 — Cmd/Ctrl+F intercept (capture-phase; the central keymap
  // bails on meta/ctrl). Mounted here so it lives only while this view is up.
  useFindHotkey();

  if (!transcriptsEnabled(env)) {
    return (
      <div className="conv-disabled">
        Transcript viewing is disabled. Enable it with
        {' '}<code>cctally config set dashboard.expose_transcripts true</code>{' '}
        (loopback is always allowed; restart the dashboard to apply).
      </div>
    );
  }

  // #217 S7 F10 — comparison takes over the workspace. On desktop the rail
  // stays visible (so the user can pick a different pair or click away — the
  // reverse-clear actions drop `compare`); on mobile the comparison goes
  // full-width (the rail is hidden, matching the single-reader mobile flow). The
  // ComparisonView itself owns its header's ✕ close (CLOSE_COMPARE).
  if (compare !== null) {
    if (isMobile) {
      return (
        <div className="conv-view conv-view--mobile conv-view--compare">
          <ComparisonView a={compare.a} b={compare.b} />
        </div>
      );
    }
    return (
      <div className="conv-view conv-view--compare">
        <ConversationRail />
        <ComparisonView a={compare.a} b={compare.b} />
      </div>
    );
  }

  // Mobile: rail until a conversation is chosen, then reader (+ back). The
  // outline rides as a slide-over SHEET (not a column) gated on the EPHEMERAL
  // convOutlineMobileOpen flag (#205 S1 — default closed, so it never
  // auto-buries the transcript), opened via the reader-head ☰ toggle. The sheet
  // carries a titled header with a visible ✕; both the ✕ and the backdrop
  // dispatch CLOSE_CONV_OUTLINE_MOBILE to dismiss it.
  if (isMobile) {
    return (
      <div className="conv-view conv-view--mobile">
        {selected == null
          ? <ConversationRail />
          : <>
              <ConversationReader sessionId={selected} outline={outline} mobileBack />
              {outlineMobileOpen && (
                <>
                  <button
                    type="button"
                    className="conv-outline-backdrop"
                    aria-label="Dismiss outline (tap outside)"
                    onClick={() => dispatch({ type: 'CLOSE_CONV_OUTLINE_MOBILE' })}
                  />
                  <div className="conv-outline-sheet">
                    <div className="conv-outline-sheet-head">
                      <span className="conv-outline-sheet-title">Outline</span>
                      <button
                        type="button"
                        className="conv-outline-close"
                        aria-label="Close outline"
                        onClick={() => dispatch({ type: 'CLOSE_CONV_OUTLINE_MOBILE' })}
                      >✕</button>
                    </div>
                    <OutlinePanel sessionId={selected} outline={outline} />
                  </div>
                </>
              )}
            </>}
      </div>
    );
  }
  const outlineVisible = outlineOpen && selected != null;
  return (
    <div
      className={['conv-view', outlineVisible ? 'conv-view--outline' : ''].filter(Boolean).join(' ')}
      // #217 S3 E6(b) — the persisted outline width feeds the 3rd grid track via
      // this custom property (only meaningful when the outline column shows).
      style={outlineVisible ? ({ ['--conv-outline-width' as string]: `${outlineWidth}px` }) : undefined}
    >
      <ConversationRail />
      {selected != null
        ? <ConversationReader sessionId={selected} outline={outline} />
        : <div className="conv-reader conv-reader--empty">
            <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true"><ChatIcon /></span>
              <div className="conv-state-title">Select a conversation</div>
              <div className="conv-state-hint">Choose one from the list to start reading.</div></div>
          </div>}
      {outlineVisible && (
        <>
          {/* #217 S3 E6(b) — the resize divider sits BETWEEN the reader body and
              the outline column (it computes width off the outline's right edge,
              which is its next sibling). */}
          <OutlineResizer />
          <OutlinePanel sessionId={selected!} outline={outline} />
        </>
      )}
    </div>
  );
}

// Module-scoped stable identity (useKeymap re-registers on array identity
// change). View gating (#156) is declared via `view:'conversations'` and
// enforced by the dispatcher; `when` carries only the transient guards.
// §4/§5 — `inView` also excludes an open filter popover so '/' and Escape don't
// fire while the popover (and its inputs) are focused, consistent with the
// reader's named-key guard (convFiltersOpen, Codex P2 #7).
const inView = () => !getState().openModal && getState().inputMode === null && !getState().convFiltersOpen;
const CONVERSATIONS_BINDINGS = [
  {
    // #177 S6 (F8) — '/' is reader-aware. With an open reader it opens the
    // floating in-conversation find bar; with no conversation selected it keeps
    // its rail-focus behavior. The `inView` guard (no open modal + no active
    // input mode) gates both, per the global-hotkeys-need-modal-guard rule.
    key: '/', scope: 'global' as const, view: 'conversations' as const, when: inView,
    action: () => {
      if (getState().selectedConversationId) {
        dispatch({ type: 'OPEN_CONV_FIND' });
        return;
      }
      const el = document.querySelector<HTMLInputElement>('.conv-rail-search input');
      el?.focus(); el?.select();
    },
  },
  {
    // #217 S3 E10#8 — reach the rail search from INSIDE an open reader without
    // pressing Esc first. `/` is reader-aware (opens the in-conversation find
    // bar when a reader is open), so `f` is the dedicated "focus the conversation
    // list search" key: it focuses `.conv-rail-search input` regardless of
    // whether a reader is open. `f` is a free slot in the conversations view —
    // the dashboard's `f` (Sessions filter) is scope:'sessions' → 'dashboard',
    // and the reader keymap's taken set (j k [ ] g o e E u U b B p P c C v n N
    // End a L m M) does not include it. Gated on the shared `inView` guard (no
    // open modal / input mode / filter popover) + the #156 conversations scope.
    key: 'f', scope: 'global' as const, view: 'conversations' as const, when: inView,
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
