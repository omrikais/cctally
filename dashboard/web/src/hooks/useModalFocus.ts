import { useEffect, useRef, type RefObject } from 'react';

interface ActiveTrap {
  token: object;
}

// Component-local overlays (notably Help over Settings) are deliberately not
// represented in the store focus-layer enum. Registration order is activation
// order, so the newest enabled trap is the only one allowed to recover focus
// that has fallen outside every card. Without this small stack, every mounted
// listener would briefly pull focus through its own lower layer before the
// topmost overlay won.
const activeTraps: ActiveTrap[] = [];

// A pointer click on a card's descriptive body does not focus the region: it
// has role=region and no tab stop. Capture the opening click before React's
// handler dispatches OPEN_MODAL, then restore to that card's real Expand button.
// Explicit controls and focusable data rows restore to themselves. This also
// covers modal openers outside cards without requiring each caller to thread a
// trigger id through every modal implementation.
interface OpeningClick {
  trigger: HTMLElement | null;
  panelKind: string | null;
}
let openingClick: OpeningClick | null = null;
let openingClickSequence = 0;

if (typeof document !== 'undefined') {
  document.addEventListener('click', (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const panel = target.closest<HTMLElement>('[data-panel-kind]');
    const control = target.closest<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [role="button"][tabindex]:not([tabindex="-1"])',
    );
    openingClick = {
      trigger: control ?? panel?.querySelector<HTMLElement>('.panel-expand:not([disabled])')
        ?? target.closest<HTMLElement>('[data-hero-strip][tabindex]') ?? null,
      panelKind: panel?.getAttribute('data-panel-kind') ?? null,
    };
    const sequence = ++openingClickSequence;
    // The candidate belongs only to this activation, never a later hotkey or
    // programmatic open. Store subscribers mount modal effects synchronously.
    setTimeout(() => {
      if (sequence === openingClickSequence) openingClick = null;
    }, 0);
  }, true);
}

function panelExpand(kind: string | null): HTMLElement | null {
  if (!kind) return null;
  const panel = Array.from(document.querySelectorAll<HTMLElement>('[data-panel-kind]'))
    .find((element) => element.getAttribute('data-panel-kind') === kind);
  return panel?.querySelector<HTMLElement>('.panel-expand:not([disabled])') ?? null;
}

function canRestoreFocus(element: HTMLElement | null): element is HTMLElement {
  return !!element && document.contains(element) && !element.hasAttribute('disabled') && !isHidden(element);
}

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
  /** Optional durable id of the trigger to restore to; falls back to the opening click or active element. */
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
  const trapTokenRef = useRef<object>({});
  // Focus-in on activate; restore on deactivate/unmount. Keyed on `active` (NOT trapEnabled),
  // so suspending under a higher layer never triggers a spurious restore.
  useEffect(() => {
    if (!active) return;
    const clicked = openingClick;
    openingClick = null;
    const trigger =
      (triggerId ? document.getElementById(triggerId) : null) ??
      clicked?.trigger ??
      (document.activeElement as HTMLElement | null);
    const triggerElementId = trigger?.id || null;
    const panelKind = clicked?.panelKind ?? trigger?.closest('[data-panel-kind]')?.getAttribute('data-panel-kind') ?? null;
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
      const restore = [
        triggerId ? document.getElementById(triggerId) : null,
        trigger,
        triggerElementId ? document.getElementById(triggerElementId) : null,
        panelExpand(panelKind),
      ].find(canRestoreFocus);
      if (restore) {
        restore.focus();
      } else {
        const activeEl = document.activeElement as HTMLElement | null;
        if (activeEl && typeof activeEl.blur === 'function') activeEl.blur();
        document.body.focus();
      }
    };
    // containerRef is stable; intentionally excluded from deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, triggerId, initialFocus]);

  // Register enabled traps separately from their key listeners. Store-tracked
  // layers normally leave exactly one entry; the stack also orders local-state
  // overlays such as Help-over-Settings without widening the store enum.
  useEffect(() => {
    if (!active || !trapEnabled) return;
    const entry: ActiveTrap = {
      token: trapTokenRef.current,
    };
    activeTraps.push(entry);
    return () => {
      const index = activeTraps.indexOf(entry);
      if (index !== -1) activeTraps.splice(index, 1);
    };
  }, [active, trapEnabled, containerRef]);

  // Tab-trap — attached only while open AND topmost.
  useEffect(() => {
    if (!active || !trapEnabled) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Tab') return;
      const container = containerRef.current;
      if (!container) return;
      const topmost = activeTraps[activeTraps.length - 1];
      if (topmost?.token !== trapTokenRef.current) return;
      const focusable = getFocusable(container);
      if (focusable.length === 0) {
        e.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;
      if (!activeEl || !container.contains(activeEl)) {
        // A control can unmount or disable while its modal remains open,
        // dropping focus to <body>; focus may also already sit on page chrome
        // behind the aria-modal card. The topmost trap owns Tab in both states.
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
      }
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
