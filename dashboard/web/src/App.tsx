import { lazy, useMemo, useSyncExternalStore } from 'react';
import { Header } from './components/Header';
import { HeroStrip } from './components/HeroStrip';
import { Footer } from './components/Footer';
import { HelpOverlay } from './components/HelpOverlay';
import { SettingsOverlay } from './components/SettingsOverlay';
import { Toast } from './components/Toast';
import { PanelHost } from './components/PanelHost';
import { PanelGridDnd } from './components/PanelGridDnd';
import { ShareModalRoot } from './share/ShareModalRoot';
import { getState, subscribeStore } from './store/store';
import { useSnapshot } from './hooks/useSnapshot';
import { useConnectionStatus } from './hooks/useConnectionStatus';
import { deriveAppState } from './lib/appState';
import { ConnectionBanner } from './components/ConnectionBanner';
import { SkeletonGrid } from './components/SkeletonGrid';
import { CARD_LAYOUT } from './lib/panelIds';
import { resolveSourceView } from './store/sourceView';
import { deriveVisiblePanelOrder } from './lib/visiblePanelOrder';
import { useBoardMode } from './hooks/useBoardMode';
import { BoardModeContext } from './lib/boardModeContext';
import { FeatureLoadBoundary } from './components/FeatureLoadBoundary';

const ConversationsView = lazy(() =>
  import('./conversations/ConversationsView').then((module) => ({
    default: module.ConversationsView,
  })),
);
const ModalRoot = lazy(() =>
  import('./modals/ModalRoot').then((module) => ({ default: module.ModalRoot })),
);
const SourceDetailModal = lazy(() =>
  import('./modals/SourceDetailModal').then((module) => ({
    default: module.SourceDetailModal,
  })),
);
const UpdateModal = lazy(() =>
  import('./components/UpdateModal').then((module) => ({
    default: module.UpdateModal,
  })),
);
const DoctorModal = lazy(() =>
  import('./components/DoctorModal').then((module) => ({
    default: module.DoctorModal,
  })),
);

export function App() {
  // Stable items array for the sortable grid. dnd-kit's rectSortingStrategy
  // handles visual reorder during drag via per-item transforms, so we don't
  // mutate the array until the drop commits via REORDER_PANELS — mutating it
  // mid-drag causes an infinite render loop (the strategy reacts to the new
  // layout, fires onDragOver again, etc.).
  const panelOrder = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.panelOrder,
  );
  // #294 S5 §6.11 — the grid renders the DERIVED visible-panel order for the
  // active source (source-hidden panels are not mounted). The persisted full
  // order (`panelOrder`) is never rewritten by a switch; DnD/keyboard reorders
  // map back into it (see the store's REORDER/SWAP handlers).
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  // Conversation viewer (spec §4). Swap the app BODY on the top-level
  // view mode; Header/Footer/overlays/modals stay outside the conditional so
  // the always-on chrome (switcher, sync chip, settings, help, doctor, toasts)
  // works in both views. ConversationsView mounts
  // its own view-aware keymap bindings only while active, so the dashboard
  // panel digits/letters can't fire over the unmounted grid.
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  const openModal = useSyncExternalStore(
    subscribeStore,
    () => getState().openModal,
  );
  const openSourceDetail = useSyncExternalStore(
    subscribeStore,
    () => getState().openSourceDetail,
  );
  const updateModalOpen = useSyncExternalStore(
    subscribeStore,
    () => getState().update.modalOpen,
  );
  const doctorModalOpen = useSyncExternalStore(
    subscribeStore,
    () => getState().doctorModalOpen,
  );
  // B2/B3 (#207): connection / bootstrap state drives the dashboard body —
  // a cold-start skeleton grid (loading), a shared error banner (failed
  // bootstrap), or the live grid with a stale banner + dim overlay when a
  // post-first-data connection drops.
  const env = useSnapshot();
  const { disconnected, bootstrapError, bootstrapMessage } = useConnectionStatus();
  const appState = deriveAppState(env, bootstrapError);
  // #293 S1 — resolve the responsive board mode ONCE here and thread it into
  // both the live grid (PanelHost data-span) and the loading skeleton, so we
  // never register ~22 duplicate MediaQueryList listeners and the load→ready
  // swap can't reflow. The mode also stamps `data-board-mode` on .dash-grid
  // for the one CSS dense-flow rule (intermediate tall-row determinism).
  const boardModeValue = useBoardMode();
  // Bento partition (#264 S1) — split panelOrder into the three height-class
  // rows by CARD_LAYOUT[id].row. Memoized on the store-stable `panelOrder`
  // reference so an SSE tick mid-drag (App re-renders on every snapshot)
  // doesn't hand PanelGridDnd a fresh `items` identity and bust its
  // drag-stability useMemo — only an actual reorder (new panelOrder ref)
  // recomputes the slices. Three PanelGridDnd instances ⇒ three dnd contexts
  // ⇒ a pointer drag can't cross height classes; each PanelHost is handed its
  // GLOBAL panelOrder index so REORDER/SWAP stay correct.
  const { tall, medium, short, globalIndex } = useMemo(() => {
    // Filter the persisted full order down to the source-visible panels, then
    // partition into the three height-class rows. The per-panel `index` handed
    // to PanelHost is its position in the VISIBLE list — SWAP_PANELS indexes the
    // visible list and writes back into the full order.
    const visibleOrder = deriveVisiblePanelOrder(panelOrder, resolveSourceView(env, activeSource));
    const byRow = (r: 'tall' | 'medium' | 'short') =>
      visibleOrder.filter((id) => CARD_LAYOUT[id].row === r);
    return {
      tall: byRow('tall'),
      medium: byRow('medium'),
      short: byRow('short'),
      globalIndex: new Map(visibleOrder.map((id, i) => [id, i])),
    };
  }, [panelOrder, env, activeSource]);
  return (
    <>
      {/* Keyboard bypass (A7) — first tab stop; reveals on :focus and
          moves focus (not just scroll) to the <main> region below. */}
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <Header />
      {/* Landmark (A2). tabIndex=-1 lets the skip-link land keyboard/SR
          focus inside the region, not merely the scroll position. */}
      <main id="main-content" tabIndex={-1}>
        {view === 'conversations' ? (
          <FeatureLoadBoundary name="Conversations">
            <ConversationsView />
          </FeatureLoadBoundary>
        ) : appState === 'loading' ? (
          <SkeletonGrid mode={boardModeValue} />
        ) : appState === 'error' ? (
          <ConnectionBanner kind="error" message={bootstrapMessage} />
        ) : (
          <>
            {disconnected && <ConnectionBanner kind="stale" />}
            {/* At-a-glance hero (#248 §1) — dashboard-only, a sibling ABOVE the
                reorderable grid. Never mounted in the conversations view or the
                loading/error branches. It scrolls away on desktop. */}
            <HeroStrip />
            {/* Bento board (#264 S1). The three height-class slices + global
                index are memoized above (drag-stable). Each row is its own
                DndContext so a pointer drag can't cross height classes. The
                stale-dim class lives on the .dash-grid wrapper. */}
            {/* #293 S3 — provide the ONE resolved board mode to every panel so
                stacked Weekly/Monthly slice their summary window via
                useContext(BoardModeContext) instead of a second useBoardMode()
                call (no duplicate matchMedia listeners, no transient
                slice-vs-grid disagreement on resize). */}
            <BoardModeContext.Provider value={boardModeValue}>
              <div className={`dash-grid${disconnected ? ' is-stale' : ''}`} data-board-mode={boardModeValue}>
                <PanelGridDnd items={tall} className="bento-row row-tall">
                  {tall.map((id) => (
                    <PanelHost key={id} id={id} index={globalIndex.get(id) ?? 0} mode={boardModeValue} />
                  ))}
                </PanelGridDnd>
                <PanelGridDnd items={medium} className="bento-row row-medium">
                  {medium.map((id) => (
                    <PanelHost key={id} id={id} index={globalIndex.get(id) ?? 0} mode={boardModeValue} />
                  ))}
                </PanelGridDnd>
                <PanelGridDnd items={short} className="bento-row row-short">
                  {short.map((id) => (
                    <PanelHost key={id} id={id} index={globalIndex.get(id) ?? 0} mode={boardModeValue} />
                  ))}
                </PanelGridDnd>
              </div>
            </BoardModeContext.Provider>
          </>
        )}
      </main>
      <Footer />
      <HelpOverlay />
      <SettingsOverlay />
      {openModal ? (
        <FeatureLoadBoundary name="Dashboard detail">
          <ModalRoot />
        </FeatureLoadBoundary>
      ) : null}
      {/* #294 S5 §5.6 — the qualified source-detail modal (Codex/All source
          rows). Renders nothing when state.openSourceDetail === null. */}
      {openSourceDetail ? (
        <FeatureLoadBoundary name="Source detail">
          <SourceDetailModal />
        </FeatureLoadBoundary>
      ) : null}
      {/* Share modal layer (spec §6.1) — separate from <ModalRoot> so
          the share modal layers ABOVE any open panel modal. Renders
          nothing when state.shareModal === null. */}
      <ShareModalRoot />
      {updateModalOpen ? (
        <FeatureLoadBoundary name="Update">
          <UpdateModal />
        </FeatureLoadBoundary>
      ) : null}
      {/* Doctor modal layer (spec §6.3) — its own `doctorModalOpen` flag
          (NOT openModal) gates the deferred mount so the composite `d` keymap guard in
          main.tsx can read it alongside update.modalOpen + inputMode
          per spec §6.4 (Codex M5). */}
      {doctorModalOpen ? (
        <FeatureLoadBoundary name="Doctor">
          <DoctorModal />
        </FeatureLoadBoundary>
      ) : null}
      <Toast />
    </>
  );
}
