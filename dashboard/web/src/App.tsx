import { useMemo, useSyncExternalStore } from 'react';
import { Header } from './components/Header';
import { HeroStrip } from './components/HeroStrip';
import { Footer } from './components/Footer';
import { HelpOverlay } from './components/HelpOverlay';
import { SettingsOverlay } from './components/SettingsOverlay';
import { Toast } from './components/Toast';
import { PanelHost } from './components/PanelHost';
import { PanelGridDnd } from './components/PanelGridDnd';
import { DoctorModal } from './components/DoctorModal';
import { UpdateModal } from './components/UpdateModal';
import { ModalRoot } from './modals/ModalRoot';
import { ShareModalRoot } from './share/ShareModalRoot';
import { ConversationsView } from './conversations/ConversationsView';
import { getState, subscribeStore } from './store/store';
import { useSnapshot } from './hooks/useSnapshot';
import { useConnectionStatus } from './hooks/useConnectionStatus';
import { deriveAppState } from './lib/appState';
import { ConnectionBanner } from './components/ConnectionBanner';
import { SkeletonGrid } from './components/SkeletonGrid';
import { CARD_LAYOUT } from './lib/panelIds';

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
  // Conversation viewer (spec §4). Swap the app BODY on the top-level
  // view mode; Header/Footer/overlays/modals stay mounted outside the
  // conditional so the always-on chrome (switcher, sync chip, settings,
  // help, doctor, toasts) works in both views. ConversationsView mounts
  // its own view-aware keymap bindings only while active, so the dashboard
  // panel digits/letters can't fire over the unmounted grid.
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  // B2/B3 (#207): connection / bootstrap state drives the dashboard body —
  // a cold-start skeleton grid (loading), a shared error banner (failed
  // bootstrap), or the live grid with a stale banner + dim overlay when a
  // post-first-data connection drops.
  const env = useSnapshot();
  const { disconnected, bootstrapError } = useConnectionStatus();
  const appState = deriveAppState(env, bootstrapError);
  // Bento partition (#264 S1) — split panelOrder into the three height-class
  // rows by CARD_LAYOUT[id].row. Memoized on the store-stable `panelOrder`
  // reference so an SSE tick mid-drag (App re-renders on every snapshot)
  // doesn't hand PanelGridDnd a fresh `items` identity and bust its
  // drag-stability useMemo — only an actual reorder (new panelOrder ref)
  // recomputes the slices. Three PanelGridDnd instances ⇒ three dnd contexts
  // ⇒ a pointer drag can't cross height classes; each PanelHost is handed its
  // GLOBAL panelOrder index so REORDER/SWAP stay correct.
  const { tall, medium, short, globalIndex } = useMemo(() => {
    const byRow = (r: 'tall' | 'medium' | 'short') =>
      panelOrder.filter((id) => CARD_LAYOUT[id].row === r);
    return {
      tall: byRow('tall'),
      medium: byRow('medium'),
      short: byRow('short'),
      globalIndex: new Map(panelOrder.map((id, i) => [id, i])),
    };
  }, [panelOrder]);
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
          <ConversationsView />
        ) : appState === 'loading' ? (
          <SkeletonGrid />
        ) : appState === 'error' ? (
          <ConnectionBanner kind="error" />
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
            <div className={`dash-grid${disconnected ? ' is-stale' : ''}`}>
              <PanelGridDnd items={tall} className="bento-row row-tall">
                {tall.map((id) => (
                  <PanelHost key={id} id={id} index={globalIndex.get(id) ?? 0} />
                ))}
              </PanelGridDnd>
              <PanelGridDnd items={medium} className="bento-row row-medium">
                {medium.map((id) => (
                  <PanelHost key={id} id={id} index={globalIndex.get(id) ?? 0} />
                ))}
              </PanelGridDnd>
              <PanelGridDnd items={short} className="bento-row row-short">
                {short.map((id) => (
                  <PanelHost key={id} id={id} index={globalIndex.get(id) ?? 0} />
                ))}
              </PanelGridDnd>
            </div>
          </>
        )}
      </main>
      <Footer />
      <HelpOverlay />
      <SettingsOverlay />
      <ModalRoot />
      {/* Share modal layer (spec §6.1) — separate from <ModalRoot> so
          the share modal layers ABOVE any open panel modal. Renders
          nothing when state.shareModal === null. */}
      <ShareModalRoot />
      <UpdateModal />
      {/* Doctor modal layer (spec §6.3) — mounted for the app's
          lifetime; its own `doctorModalOpen` flag (NOT openModal)
          gates the chrome so the composite `d` keymap guard in
          main.tsx can read it alongside update.modalOpen + inputMode
          per spec §6.4 (Codex M5). */}
      <DoctorModal />
      <Toast />
    </>
  );
}
