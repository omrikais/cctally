export type KeymapScope = 'overlay' | 'global' | 'sessions' | 'modal';

export interface Binding {
  key: string;
  scope: KeymapScope;
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

    const ordered = [...bindings].sort((a, b) => SCOPE_ORDER[a.scope] - SCOPE_ORDER[b.scope]);
    for (const b of ordered) {
      if (b.key !== e.key) continue;
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
