import { useState, useSyncExternalStore } from 'react';
import { useKeymap } from '../hooks/useKeymap';
import { getState, subscribeStore } from '../store/store';
import { PANEL_REGISTRY, type PanelId } from '../lib/panelRegistry';
import { useIsMobile } from '../hooks/useIsMobile';

interface KeyTableProps {
  panelOrder: readonly PanelId[];
}

function KeyTable({ panelOrder }: KeyTableProps) {
  return (
    <table>
      <tbody>
        {panelOrder.map((id, idx) => (
          <tr key={id}>
            <td><kbd>{idx + 1}</kbd></td>
            <td>Open {PANEL_REGISTRY[id].label} modal</td>
          </tr>
        ))}
        <tr><td><kbd>r</kbd></td><td>force refresh</td></tr>
        <tr><td><kbd>s</kbd></td><td>open Settings</td></tr>
        <tr><td>Hold + drag a card</td><td>rearrange the dashboard</td></tr>
        <tr><td><kbd>Shift</kbd>+<kbd>↑</kbd>/<kbd>↓</kbd></td><td>swap focused card with neighbor</td></tr>
        <tr><td><kbd>↑</kbd>/<kbd>↓</kbd></td><td>Select period (Weekly/Monthly/Daily modal)</td></tr>
        <tr><td><kbd>?</kbd></td><td>toggle this help</td></tr>
        <tr><td><kbd>Esc</kbd></td><td>close</td></tr>
      </tbody>
    </table>
  );
}

function GestureGuide() {
  return (
    <ul className="help-gesture-list">
      <li><strong>Tap the chevron</strong> — expand or collapse a panel</li>
      <li><strong>Long-press a panel</strong> — drag to rearrange the dashboard</li>
      <li><strong>Tap ✕ or the dim backdrop</strong> — close a sheet (or <kbd>Esc</kbd> on tablets)</li>
      <li><strong>Tap a model chip</strong> — filter Sessions by that model</li>
      <li><strong>Tap the sync chip</strong> — force a fresh refresh</li>
    </ul>
  );
}

export function HelpOverlay() {
  const [open, setOpen] = useState(false);
  const isMobile = useIsMobile();
  const panelOrder = useSyncExternalStore(subscribeStore, () => getState().prefs.panelOrder);
  useKeymap([
    { key: '?', scope: 'global', action: () => setOpen((o) => !o) },
    { key: 'Escape', scope: 'global', action: () => setOpen(false), when: () => open },
  ]);
  if (!open) return null;
  return (
    <div id="help-overlay" onClick={() => setOpen(false)}>
      <div className="help-card" onClick={(e) => e.stopPropagation()}>
        <div className="help-header">
          <h2>{isMobile ? 'Help' : 'Keybindings'}</h2>
          <button
            className="modal-close"
            type="button"
            aria-label="Close"
            onClick={() => setOpen(false)}
          >
            ×
          </button>
        </div>
        {isMobile ? (
          <>
            <GestureGuide />
            <details className="help-keyboard-disclosure">
              <summary>Keyboard shortcuts ▾</summary>
              <KeyTable panelOrder={panelOrder} />
            </details>
          </>
        ) : (
          <KeyTable panelOrder={panelOrder} />
        )}
        <p className="meta">
          cctally · <span id="help-server-url">{window.location.origin}</span>
        </p>
      </div>
    </div>
  );
}
