import { getState } from './store';

export type KeymapScope = 'overlay' | 'global' | 'sessions' | 'modal';
export type KeymapView = 'dashboard' | 'conversations' | 'any';

export interface Binding {
  key: string;
  scope: KeymapScope;
  // View eligibility (#156). Omitted → `defaultView(scope)`. 'any' = the
  // view-agnostic chrome (Settings/Help/Doctor + any open modal/overlay).
  view?: KeymapView;
  // Intra-scope tiebreaker (#159). Higher fires first, mirroring CSS z-index.
  // Default 0; only consulted to break a same-scope, same-key tie (e.g. two
  // overlay-scope Esc handlers open at once). SCOPE_ORDER stays primary.
  layer?: number;
  action: () => void;
  when?: () => boolean;
}

const bindings = new Set<Binding>();
let handler: ((e: KeyboardEvent) => void) | null = null;

// `overlay` sits ABOVE `modal` so an overlay-scope Esc handler (e.g. the
// share modal layered on top of a panel modal) fires before the panel
// modal's Esc handler — preserving the spec §12.1 "Esc closes the
// topmost overlay" invariant when both surfaces are open simultaneously.
const SCOPE_ORDER: Record<KeymapScope, number> = { overlay: 0, modal: 1, sessions: 2, global: 3 };

// Default view per scope (#156). Positional scopes (global, sessions) are
// dashboard-bound unless a binding opts out — a forgotten `view` therefore
// confines a binding to the dashboard and can never leak into the
// conversations view. Layering scopes (overlay, modal) default to 'any': an
// open modal/overlay owns its keys regardless of the view beneath it. #158
// made both view-entry reducers (SET_VIEW + OPEN_CONVERSATION) clear openModal
// + the share/composer slots, so a panel modal no longer survives a workspace
// switch — but the 'any' default stays as defense-in-depth, keeping any modal
// closable regardless of the view beneath it. See docs/dashboard-gotchas.md.
function defaultView(scope: KeymapScope): KeymapView {
  return scope === 'modal' || scope === 'overlay' ? 'any' : 'dashboard';
}

function isTextInputFocused(target: EventTarget | null): boolean {
  if (!target || !(target instanceof Element)) return false;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true;
  if (target instanceof HTMLElement) {
    if (target.isContentEditable) return true;
    // jsdom doesn't fully implement isContentEditable; check the attribute too.
    const ce = target.getAttribute('contenteditable');
    if (ce === '' || ce === 'true' || ce === 'plaintext-only') return true;
  }
  return false;
}

export function registerKeymap(list: Binding[]): () => void {
  list.forEach((b) => bindings.add(b));
  return () => list.forEach((b) => bindings.delete(b));
}

export function installGlobalKeydown(): void {
  if (handler) return;
  handler = (e: KeyboardEvent): void => {
    // Bail on OS/browser modifier shortcuts so the native action (tab switch,
    // find, reload, etc.) is never shadowed by a bound letter/digit.
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    // Input-mode suppression: single-char keys are swallowed when focus is in
    // an INPUT/TEXTAREA/contenteditable. Escape, Enter, and other named keys
    // pass through so modals/input-owners can close/confirm.
    if (isTextInputFocused(e.target) && e.key.length === 1) return;

    const currentView = getState().view;
    const ordered = [...bindings].sort((a, b) => {
      const byScope = SCOPE_ORDER[a.scope] - SCOPE_ORDER[b.scope];
      if (byScope !== 0) return byScope;
      // Same scope: higher layer (z-index) fires first (#159). Layerless
      // bindings (default 0) tie here and keep stable-sort insertion order.
      return (b.layer ?? 0) - (a.layer ?? 0);
    });
    for (const b of ordered) {
      if (b.key !== e.key) continue;
      // Central view gate (#156): a binding fires only in its view (or 'any').
      const bv = b.view ?? defaultView(b.scope);
      if (bv !== 'any' && bv !== currentView) continue;
      if (b.when && !b.when()) continue;
      b.action();
      e.preventDefault();
      return;
    }
  };
  document.addEventListener('keydown', handler);
}

export function uninstallGlobalKeydown(): void {
  if (!handler) return;
  document.removeEventListener('keydown', handler);
  handler = null;
}

export function _resetForTests(): void {
  bindings.clear();
  if (handler) document.removeEventListener('keydown', handler);
  handler = null;
}
