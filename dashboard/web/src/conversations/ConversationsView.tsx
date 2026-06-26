import { useEffect, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { useKeymap } from '../hooks/useKeymap';
import { useIsMobile } from '../hooks/useIsMobile';
import { useIsWide } from '../hooks/useIsWide';
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
  // #228 S3 F1 — the outline rides as a SHEET across the whole no-column band
  // (≤1100px, keyed on !isWide), not just on mobile (≤640px). The persistent
  // column + resizer render only when wide. Distinct from `isMobile` (the
  // single-pane / 44px-target / two-row-header cutover) — see §8.
  const isWide = useIsWide();
  // #228 S3 F1 (Codex P3) — `convOutlineMobileOpen` is ephemeral but only resets
  // on a conversation switch, so a tablet→wide→tablet resize would resurrect the
  // sheet. Close it on the isWide rising edge (the column takes over when wide).
  const prevWideRef = useRef(isWide);
  useEffect(() => {
    if (isWide && !prevWideRef.current) dispatch({ type: 'CLOSE_CONV_OUTLINE_MOBILE' });
    prevWideRef.current = isWide;
  }, [isWide]);
  // #177 S5 — full-session outline + stats for the selected conversation. The
  // hook owns its own SSE-tick revalidation; a null `selected` yields a null
  // outline. Shared with the reader (toggle button + scroll-sync registration in
  // Tasks 4/5) and the OutlinePanel (third grid column / mobile slide-over).
  // #227 — pass null while a comparison is open: the compare branch returns
  // before `outline` is consumed, so this hook would otherwise double-fetch A's
  // outline (selected === compare.a) alongside ComparisonView's own A hook.
  const { outline } = useConversationOutline(compare !== null ? null : selected);

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

  // #228 S3 F1 — the outline slide-over SHEET, rendered whenever the persistent
  // column is hidden (≤1100px = !isWide). Gated on the EPHEMERAL
  // convOutlineMobileOpen flag (#205 S1 — default closed, so it never
  // auto-buries the transcript), opened via the reader-head ☰ toggle. The sheet
  // carries a titled header with a visible ✕; both the ✕ and the backdrop
  // dispatch CLOSE_CONV_OUTLINE_MOBILE to dismiss it. Shared by the mobile
  // single-pane branch AND the desktop two-pane branch below (the tablet band
  // 641–1100 keeps the two-pane shell but gets the sheet, not the column).
  const outlineSheet = (sid: string) => (
    outlineMobileOpen && (
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
          <OutlinePanel sessionId={sid} outline={outline} />
        </div>
      </>
    )
  );

  // Mobile: rail until a conversation is chosen, then reader (+ back) with the
  // outline as the slide-over sheet (always a sheet ≤640px since !isWide holds).
  if (isMobile) {
    return (
      <div className="conv-view conv-view--mobile">
        {selected == null
          ? <ConversationRail />
          : <>
              <ConversationReader sessionId={selected} outline={outline} mobileBack />
              {outlineSheet(selected)}
            </>}
      </div>
    );
  }
  // #228 S3 F1 — the persistent column shows ONLY when wide (≥1101px); ≤1100px
  // the desktop two-pane shell keeps the rail + reader but the outline rides as
  // the slide-over sheet (the tablet-band ☰ is now a live control, not a lie).
  const outlineVisible = isWide && outlineOpen && selected != null;
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
      {/* #228 S3 F1 — the tablet-band (641–1100) outline sheet, rendered when the
          column is hidden (!isWide) and a conversation is selected. */}
      {!isWide && selected != null && outlineSheet(selected)}
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
    // #228 S4 D1 — gate Escape on the shared `inView` guard (mirroring '/' and
    // 'f'), not the old `!openModal`-only guard. Escape is a NAMED key, so the
    // keymap dispatcher does NOT auto-suppress it while an input is focused; the
    // old guard let a rail-search Escape fire BOTH the input's own clear-and-blur
    // AND this global binding — which then saw the now-empty needle and ejected
    // the whole workspace to the dashboard. `inView` excludes inputMode !== null
    // and an open filters popover, so Esc-while-typing no longer reaches here; Esc
    // with nothing focused keeps the intended two-step (clear-then-exit).
    key: 'Escape', scope: 'global' as const, view: 'conversations' as const,
    when: inView,
    action: () => {
      // #238 S3 (C2) — a comparison is the dominant foreground overlay, so Escape
      // dismisses it FIRST, back to the reader (NOT the dashboard). Mirror ✕ Close
      // exactly: CLOSE_COMPARE clears `compare` AND arms compareCloseFocusPending
      // so the reader returns focus to #conv-compare-with. Ordering (resolved Q1):
      // comparison-close wins over rail-search-clear — which also fixes mobile,
      // where the rail/search is hidden during a comparison so clearing a stale
      // needle would look like a no-op and leave the comparison open. OPEN_COMPARE
      // clears convFiltersOpen, so inView is true here and this branch can fire.
      if (getState().compare) { dispatch({ type: 'CLOSE_COMPARE' }); return; }
      if (getState().conversationSearch) { dispatch({ type: 'SET_CONVERSATION_SEARCH', text: '' }); return; }
      dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    },
  },
];
