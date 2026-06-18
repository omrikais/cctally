import { useEffect, type RefObject } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'summary',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

// Layout-independent visibility check. We deliberately do NOT key on
// `offsetParent`/`getClientRects()` (both report 0/empty under jsdom — the
// repo's known no-layout test gap), which would make every focusable element
// look hidden in tests. Instead we reject only elements that are explicitly
// hidden: the `hidden` attribute, `aria-hidden="true"`, or a computed
// `display:none`/`visibility:hidden` (jsdom honors getComputedStyle for
// inline + stylesheet rules). A `display:none` ancestor also zeroes the
// child's computed `display`, so an off-screen subtree is still excluded.
function isHidden(el: HTMLElement): boolean {
  if (el.hasAttribute('hidden')) return true;
  if (el.getAttribute('aria-hidden') === 'true') return true;
  if (el.closest('[hidden],[aria-hidden="true"]')) return true;
  const style =
    typeof window !== 'undefined' && typeof window.getComputedStyle === 'function'
      ? window.getComputedStyle(el)
      : null;
  if (style && (style.display === 'none' || style.visibility === 'hidden')) {
    return true;
  }
  return false;
}

function getFocusable(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  ).filter((el) => !el.hasAttribute('disabled') && !isHidden(el));
}

export interface UseModalFocusOptions {
  /** Is this surface open at all (drives focus-in + restore). */
  active: boolean;
  /** Is this surface the topmost focus-managed layer (drives the Tab-trap). Default true. */
  trapEnabled?: boolean;
  /** Optional id of the trigger to restore to; falls back to document.activeElement at open. */
  triggerId?: string;
}

/**
 * Hand-rolled modal focus management: move focus in on open, trap Tab/Shift+Tab
 * at the boundaries (only while topmost), restore focus to the trigger on close.
 * Esc is NOT handled here — the keymap owns it.
 */
export function useModalFocus(
  containerRef: RefObject<HTMLElement>,
  { active, trapEnabled = true, triggerId }: UseModalFocusOptions,
): void {
  // Focus-in on activate; restore on deactivate/unmount. Keyed on `active` (NOT trapEnabled),
  // so suspending under a higher layer never triggers a spurious restore.
  useEffect(() => {
    if (!active) return;
    const trigger =
      (triggerId ? document.getElementById(triggerId) : null) ??
      (document.activeElement as HTMLElement | null);
    const container = containerRef.current;
    if (container) {
      const focusable = getFocusable(container);
      (focusable[0] ?? container).focus();
    }
    return () => {
      if (trigger && typeof trigger.focus === 'function' && document.contains(trigger)) {
        trigger.focus();
      } else {
        const activeEl = document.activeElement as HTMLElement | null;
        if (activeEl && typeof activeEl.blur === 'function') activeEl.blur();
        document.body.focus();
      }
    };
    // containerRef is stable; intentionally excluded from deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, triggerId]);

  // Tab-trap — attached only while open AND topmost.
  useEffect(() => {
    if (!active || !trapEnabled) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Tab') return;
      const container = containerRef.current;
      if (!container) return;
      // Belt-and-suspenders: if focus already lives outside (a higher/local-state
      // layer owns it, e.g. Help/Settings), do nothing.
      if (!container.contains(document.activeElement)) return;
      const focusable = getFocusable(container);
      if (focusable.length === 0) {
        e.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;
      if (e.shiftKey && activeEl === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && activeEl === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, trapEnabled]);
}
