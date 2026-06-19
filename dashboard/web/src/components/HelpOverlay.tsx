import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useKeymap } from '../hooks/useKeymap';
import { useModalFocus } from '../hooks/useModalFocus';
import { dispatch, getState, subscribeStore } from '../store/store';
import { PANEL_REGISTRY, type PanelId } from '../lib/panelRegistry';
import { useIsMobile } from '../hooks/useIsMobile';

interface KeyTableProps {
  panelOrder: readonly PanelId[];
}

// Data-driven non-positional shortcut rows (#207 D1). Single-key
// global/sessions bindings live HERE so the keybindings help can never drift
// out of sync with the live keymap — HelpOverlay.coverage.test.tsx fails RED
// if a future global/sessions hotkey lands without a row. The digit/positional
// panel-open rows (`panelOrder`-derived) and the multi-key combo rows
// (Hold+drag, Shift+↑/↓, ↑/↓) are NOT single registered keys and stay rendered
// separately below. `ConversationsKeyTable` (view:'conversations' keys) is
// excluded from the coverage assertion and stays untouched.
export const HELP_ROWS: ReadonlyArray<{ keys: string[]; desc: string }> = [
  { keys: ['r'], desc: 'force refresh' },
  { keys: ['s'], desc: 'open Settings' },
  { keys: ['d'], desc: 'open Doctor' },
  { keys: ['S'], desc: 'share the focused panel (focus a panel first)' },
  { keys: ['B'], desc: 'open the report composer' },
  { keys: ['f'], desc: 'filter Sessions' },
  { keys: ['/'], desc: 'search Sessions' },
  { keys: ['c'], desc: 'collapse / expand the Sessions panel' },
  { keys: ['n', 'N'], desc: 'next / previous search match' },
  { keys: ['q'], desc: 'quit (close the tab)' },
  { keys: ['?'], desc: 'toggle this help' },
  { keys: ['Esc'], desc: 'close' },
];

function KeyTable({ panelOrder }: KeyTableProps) {
  return (
    <table>
      <tbody>
        {panelOrder.map((id, idx) => (
          <tr key={id}>
            {/* Positions 1..10 are bound to digits 1, 2, …, 9, 0 in
                main.tsx (position 10 renders '0', not '10'). Positions
                ≥ 11 have NO digit binding — render an em-dash instead
                of a literal "11". Spec 2026-05-21 §1 defers multi-key
                chord support to F9. The rule is positional, not panel-
                id-specific. */}
            <td>
              {idx < 9 ? (
                <kbd>{String(idx + 1)}</kbd>
              ) : idx === 9 ? (
                <kbd>0</kbd>
              ) : (
                <span aria-hidden="true">—</span>
              )}
            </td>
            <td>Open {PANEL_REGISTRY[id]?.label ?? id} modal</td>
          </tr>
        ))}
        {HELP_ROWS.map((row) => (
          <tr key={row.desc}>
            <td>
              {row.keys
                .map<ReactNode>((k) => <kbd key={k}>{k}</kbd>)
                .reduce((acc, el) => [acc, ' / ', el])}
            </td>
            <td>{row.desc}</td>
          </tr>
        ))}
        {/* Multi-key combos / gestures — not single registered keys, so they
            stay hand-written and are excluded from the coverage assertion. */}
        <tr><td>Hold + drag a card</td><td>rearrange the dashboard</td></tr>
        <tr><td><kbd>Shift</kbd>+<kbd>↑</kbd>/<kbd>↓</kbd></td><td>swap focused card with neighbor</td></tr>
        <tr><td><kbd>↑</kbd>/<kbd>↓</kbd></td><td>Select period (Weekly/Monthly/Daily modal)</td></tr>
      </tbody>
    </table>
  );
}

// G3 — the reader keys, shown only in the conversations view (mirrors how
// the bindings themselves are `view:'conversations'`-scoped).
function ConversationsKeyTable() {
  return (
    <table className="help-conversations">
      <tbody>
        <tr><td colSpan={2}><strong>Conversations</strong></td></tr>
        <tr><td><kbd>j</kbd> / <kbd>k</kbd></td><td>move turns</td></tr>
        <tr><td><kbd>[</kbd> / <kbd>]</kbd></td><td>collapse / expand all</td></tr>
        <tr><td><kbd>g</kbd></td><td>jump to top</td></tr>
        <tr><td><kbd>e</kbd> / <kbd>E</kbd></td><td>next / prev error</td></tr>
        <tr><td><kbd>u</kbd> / <kbd>U</kbd></td><td>next / prev prompt</td></tr>
        <tr><td><kbd>b</kbd> / <kbd>B</kbd></td><td>next / prev subagent</td></tr>
        <tr><td><kbd>p</kbd> / <kbd>P</kbd></td><td>next / prev plan / question</td></tr>
        <tr><td><kbd>c</kbd> / <kbd>C</kbd></td><td>next / prev cache rebuild</td></tr>
        <tr><td><kbd>o</kbd></td><td>toggle outline</td></tr>
        <tr><td><kbd>v</kbd></td><td>cycle focus mode</td></tr>
        <tr><td><kbd>/</kbd></td><td>search conversations</td></tr>
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
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  useKeymap([
    // `?` is all-views chrome (#156).
    { key: '?', scope: 'global', view: 'any', action: () => setOpen((o) => !o) },
    // Esc at `overlay` scope (z-index 1000 = topmost): SCOPE_ORDER beats the
    // conversations-view `global` Esc (#156); layer 1000 beats any lower
    // overlay (share/composer) Esc on a same-scope tie, regardless of
    // registration order (#159). Mirrors `#help-overlay { z-index: 1000 }`.
    { key: 'Escape', scope: 'overlay', layer: 1000, action: () => setOpen(false), when: () => open },
  ]);
  // a11y focus management (#207 A1). Help is a local-state overlay that can open
  // over anything (z-index 1000); it moves focus into itself, and the
  // contains-guard in `useModalFocus` keeps every lower surface from fighting.
  // `trapEnabled` defaults to true. Called BEFORE the `!open` early-return so
  // the hook order stays stable (Rules of Hooks).
  const cardRef = useRef<HTMLDivElement>(null);
  useModalFocus(cardRef, { active: open });
  // #207 D2: while Help is open, the always-on hotkeys (digits, r/q/n/N,
  // c/S/B/f//) must be inert. Help is component-local and invisible to the
  // store's modal fields, so it explicitly tracks itself via a depth counter.
  // Declared BEFORE the `!open` early-return so the hook order stays stable.
  useEffect(() => {
    if (!open) return;
    dispatch({ type: 'INCREMENT_CHROME_OVERLAY' });
    return () => dispatch({ type: 'DECREMENT_CHROME_OVERLAY' });
  }, [open]);
  if (!open) return null;
  return (
    <div id="help-overlay" onClick={() => setOpen(false)}>
      <div ref={cardRef} className="help-card" onClick={(e) => e.stopPropagation()}>
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
              {view === 'conversations' && <ConversationsKeyTable />}
            </details>
          </>
        ) : (
          <>
            <KeyTable panelOrder={panelOrder} />
            {view === 'conversations' && <ConversationsKeyTable />}
          </>
        )}
        <p className="meta">
          cctally · <span id="help-server-url">{window.location.origin}</span>
        </p>
      </div>
    </div>
  );
}
