import { dispatch, getState } from './store';
import { triggerSync } from './sync';
import { stepMatch, tryQuit } from './actions';
import { openPanelByPosition } from '../lib/openPanelByPosition';
import { buildShareKeyBinding } from '../share/keyboardShare';
import { buildBasketKeyBindings } from '../share/keyboardBasket';
import { BENTO_MEDIA_QUERY } from '../lib/breakpoints';
import type { Binding } from './keymap';
import type { DashboardSelection } from '../types/envelope';

// #294 S5 — the `v` cycle order for the global source selector. Dashboard-view
// scoped; the Conversations `v` (cycle focus mode) is view:'conversations' and
// unaffected. Guarded by `_globalKeyGuard` (inert under any modal / input mode /
// chrome overlay).
const SOURCE_CYCLE: readonly DashboardSelection[] = ['claude', 'codex', 'all'];

export function cycleActiveSource(): void {
  const cur = getState().activeSource;
  const idx = SOURCE_CYCLE.indexOf(cur);
  const next = SOURCE_CYCLE[(idx + 1) % SOURCE_CYCLE.length];
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: next });
}

// True in the desktop bento (>=900px), where per-card collapse is removed (A3).
// SSR/JSDOM-safe: no matchMedia → treat as "not bento" so `c` behaves as before.
function _isDesktopBento(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia
    && window.matchMedia(BENTO_MEDIA_QUERY).matches;
}

// The always-on dashboard key bindings, extracted out of main.tsx into a
// side-effect-free builder (#207 D1) so the drift-guard test can register
// the production bindings without booting SSE / createRoot. main.tsx calls
// `registerKeymap(buildGlobalKeyBindings())`; the result is the identical
// binding list it inlined before.

// All digit/letter globals are guarded against modals layered in their
// own root (`update.modalOpen`, `doctorModalOpen`) so a keystroke
// behind one of those modals doesn't dispatch a parallel panel modal
// into ModalRoot or fire `q`/`r` invisibly underneath. The Update and
// Doctor modals each manage their own `Escape` via modal-scope bindings,
// so closing them is unaffected by this guard. See
// `gotcha: project_global_hotkeys_modal_guard`.
//
// #207 D2: the guard now also blocks a panel modal (`openModal`), the
// search/filter input modes (`inputMode`), and the component-local chrome
// overlays Settings/Help (`chromeOverlayOpen`) — none of which it checked
// before, so `r`/`q`/`n`/`N`/digits used to fire underneath all of them.
export function _globalKeyGuard(): boolean {
  const s = getState();
  // Modal-layering guard only — the view gate (#156) now lives in the keymap
  // dispatcher (these panel globals are scope:'global' → default 'dashboard').
  return !s.openModal && !s.update.modalOpen && !s.doctorModalOpen
    && s.inputMode === null && s.chromeOverlayOpen === 0;
}

// Doctor key (`d`) uses a composite guard per spec §6.4 (Codex M5):
// - !openModal      — a panel modal isn't currently up.
// - !update.modalOpen — the update modal (own root) isn't up.
// - inputMode === null — search/filter input modes own the keyboard.
// - chromeOverlayOpen === 0 — Settings/Help aren't up (#207 D2).
// The bare _globalKeyGuard pattern would let `d` fire through a
// panel modal or during text-input mode. The same triple guard the
// share/composer/basket keys use (see share/keyboardShare.ts).
export function _doctorOpenGuard(): boolean {
  const s = getState();
  if (s.openModal !== null) return false;
  if (s.update.modalOpen) return false;
  if (s.inputMode !== null) return false;
  if (s.chromeOverlayOpen !== 0) return false;
  return true;
}

export function buildGlobalKeyBindings(): Binding[] {
  return [
    { key: '1', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(1) },
    { key: '2', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(2) },
    { key: '3', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(3) },
    { key: '4', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(4) },
    { key: '5', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(5) },
    { key: '6', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(6) },
    { key: '7', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(7) },
    { key: '8', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(8) },
    { key: '9', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(9) },
    // 10th panel — `0` follows the keyboard-shortcut "10 wraps to 0"
    // convention (mirrors how vim / many TUIs map digit keys). The same
    // `_globalKeyGuard` blocks it while update/doctor modals are open.
    { key: '0', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(10) },
    { key: 'r', scope: 'global', when: _globalKeyGuard, action: () => triggerSync() },
    // #294 S5 — cycle the global source (Claude → Codex → All → Claude). Same
    // modal/input guard as the other dashboard globals. Dashboard-view default
    // (the Conversations `v` cycles focus mode and is view-scoped).
    { key: 'v', scope: 'global', when: _globalKeyGuard, action: cycleActiveSource },
    { key: 'q', scope: 'global', when: _globalKeyGuard, action: tryQuit },
    { key: 'n', scope: 'global', when: _globalKeyGuard, action: () => stepMatch(1) },
    { key: 'N', scope: 'global', when: _globalKeyGuard, action: () => stepMatch(-1) },
    // Doctor modal — composite guard (see _doctorOpenGuard above).
    // `view:'any'` (#156): all-views chrome; without it this scope:'global'
    // binding would regress to dashboard-only.
    { key: 'd', scope: 'global', view: 'any', when: _doctorOpenGuard, action: () => dispatch({ type: 'OPEN_DOCTOR_MODAL' }) },
    // Share v2 (spec §12.1). Opens the share modal for the focused panel.
    // Guards (composer/share/panel modals empty, no input mode, focus on a
    // share-capable panel, not mobile) live inside buildShareKeyBinding so
    // tests can drive them through the same module main.tsx wires up.
    buildShareKeyBinding(),
    // Share v2 (spec §12.1). `B` opens the composer modal. Same guard
    // surface as `S` except the composer is global (no panel focus
    // resolution). Uppercase-only (mirrors the `S`-vs-`s` precedent;
    // lowercase `b` stays free for future per-panel use).
    ...buildBasketKeyBindings(),
    {
      key: 'c',
      scope: 'sessions',
      // scope:'sessions' → default 'dashboard'; the dispatcher gates the view.
      // chromeOverlayOpen === 0 keeps `c` inert under Settings/Help (#207 D2).
      // #264 S4 (A3): !_isDesktopBento() makes `c` inert >=900px, matching the
      // hidden collapse chevron — desktop `c` must not mutate sessionsCollapsed.
      when: () => !getState().openModal && getState().chromeOverlayOpen === 0 && !_isDesktopBento(),
      action: () => {
        const cur = getState().prefs.sessionsCollapsed;
        dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: !cur } });
      },
    },
  ];
}
