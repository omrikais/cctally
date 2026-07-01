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
// hidden: the `hidden` attribute, `aria-hidden="true"`, an `inert` subtree,
// or a computed `display:none`/`visibility:hidden` (jsdom honors
// getComputedStyle for inline + stylesheet rules). A `display:none` ancestor
// also zeroes the child's computed `display`, so an off-screen subtree is
// still excluded. `inert` is included so a nested confirm/alertdialog that
// marks the rest of the dialog `inert` (e.g. SettingsOverlay's discard guard,
// #252) leaves getFocusable returning ONLY the confirm's controls — the
// card-level trap then cycles just those, matching native `inert` focus
// removal. The `el.inert` IDL property reflects to the `[inert]` attribute, so
// the `closest('[inert]')` match covers imperatively-set inert nodes too.
function isHidden(el: HTMLElement): boolean {
  if (el.hasAttribute('hidden')) return true;
  if (el.getAttribute('aria-hidden') === 'true') return true;
  if (el.closest('[hidden],[aria-hidden="true"],[inert]')) return true;
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
  /**
   * Where to move focus on open. Default `'first'` focuses the first focusable
   * (the standard a11y move). `'container'` focuses the dialog container itself
   * — needed when the first control self-disables on open (a focused element
   * that becomes `disabled` is blurred by the browser, dropping focus to
   * `<body>`). The container is `tabIndex=-1`, so it can never be disabled.
   * `'heading'` focuses the dialog heading (the `aria-labelledby` target, e.g.
   * `#modal-title`) so the reader lands on the modal's title/answer rather than
   * a header affordance (SH-2). The heading must carry `tabIndex={-1}` for
   * `.focus()` to take — the FOCUSABLE_SELECTOR deliberately excludes
   * `[tabindex="-1"]`, so heading focus needs this explicit path, and it stays
   * out of the Tab order. Falls back to `'first'` when no heading is present.
   */
  initialFocus?: 'first' | 'container' | 'heading';
}

/**
 * Hand-rolled modal focus management: move focus in on open, trap Tab/Shift+Tab
 * at the boundaries (only while topmost), restore focus to the trigger on close.
 * Esc is NOT handled here — the keymap owns it.
 */
export function useModalFocus(
  containerRef: RefObject<HTMLElement>,
  { active, trapEnabled = true, triggerId, initialFocus = 'first' }: UseModalFocusOptions,
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
      if (initialFocus === 'container') {
        // Focus the container itself (tabIndex=-1 so it can't be disabled).
        container.focus();
      } else if (initialFocus === 'heading') {
        // Focus the dialog heading (aria-labelledby target). It carries
        // tabIndex={-1} so `.focus()` takes yet it stays out of the Tab order
        // and out of getFocusable(). Fall back to the first focusable, then the
        // container, if no heading exists.
        const heading = container.querySelector<HTMLElement>(
          '[data-modal-heading], #modal-title, h2',
        );
        (heading ?? getFocusable(container)[0] ?? container).focus();
      } else {
        const focusable = getFocusable(container);
        (focusable[0] ?? container).focus();
      }
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
  }, [active, triggerId, initialFocus]);

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
      const idx = activeEl ? focusable.indexOf(activeEl) : -1;
      if (idx === -1) {
        // Focus is inside the container but NOT on a focusable — e.g. on the
        // container itself when `initialFocus: 'container'` is used (the
        // container is tabIndex=-1, so it's absent from `focusable`). Drive Tab
        // to the first focusable and Shift+Tab to the last, so neither edge
        // escapes the dialog. (We already returned early above if focus lives
        // outside the container, so reaching here means focus is inside it.)
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && activeEl === first) {
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
