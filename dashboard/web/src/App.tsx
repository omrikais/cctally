import { useSyncExternalStore } from 'react';
import { Header } from './components/Header';
import { Footer } from './components/Footer';
import { HelpOverlay } from './components/HelpOverlay';
import { SettingsOverlay } from './components/SettingsOverlay';
import { Toast } from './components/Toast';
import { PanelHost } from './components/PanelHost';
import { PanelGridDnd } from './components/PanelGridDnd';
import { UpdateModal } from './components/UpdateModal';
import { ModalRoot } from './modals/ModalRoot';
import { getState, subscribeStore } from './store/store';

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
  return (
    <>
      <Header />
      <PanelGridDnd items={panelOrder}>
        <div className="grid">
          {panelOrder.map((id, index) => (
            <PanelHost key={id} id={id} index={index} />
          ))}
        </div>
      </PanelGridDnd>
      <Footer />
      <HelpOverlay />
      <SettingsOverlay />
      <ModalRoot />
      <UpdateModal />
      <Toast />
    </>
  );
}
