import { useSyncExternalStore } from 'react';
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
import { CARD_TIER } from './lib/panelIds';

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
            {(() => {
              // Two-tier grid (#248 §2). Partition the single prefs.panelOrder
              // into a TILE slice (uniform compact summary cards) and a WIDE
              // slice (full-width content-height data cards), preserving each
              // tier's relative order. Two PanelGridDnd instances ⇒ two dnd
              // contexts ⇒ a pointer drag can't cross tiers; each PanelHost is
              // handed its GLOBAL panelOrder index so REORDER/SWAP stay correct.
              // The stale-dim class lives on the .dash-grid wrapper now.
              const tiles = panelOrder.filter((id) => CARD_TIER[id] === 'tile');
              const wides = panelOrder.filter((id) => CARD_TIER[id] === 'wide');
              const globalIndex = new Map(panelOrder.map((id, i) => [id, i]));
              return (
                <div className={`dash-grid${disconnected ? ' is-stale' : ''}`}>
                  <PanelGridDnd items={tiles} className="tile-strip">
                    {tiles.map((id) => (
                      <PanelHost key={id} id={id} index={globalIndex.get(id) ?? 0} />
                    ))}
                  </PanelGridDnd>
                  <PanelGridDnd items={wides} className="wide-strip">
                    {wides.map((id) => (
                      <PanelHost key={id} id={id} index={globalIndex.get(id) ?? 0} />
                    ))}
                  </PanelGridDnd>
                </div>
              );
            })()}
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
