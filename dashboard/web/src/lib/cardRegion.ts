import type React from 'react';

// #293 S4 — the single double-fire guard for card-region pointer clicks.
// A bento card <section role="region"> keeps a body-click convenience where it
// has one today, but a click inside an interactive control or explicitly inert
// explanatory content must not open the panel modal. The same ignore marker is
// used for the drag grip, legends, aggregate tails and provider section labels.
// Pure leaf — no store import.
const INTERACTIVE_SELECTOR =
  'button, a, input, select, textarea, [role="button"], [data-card-region-ignore]';

export function isInteractiveActivationTarget(target: EventTarget | null): boolean {
  const el = target as Element | null;
  // Element.closest exists on HTMLElement AND SVGElement, so an icon-glyph
  // click inside a <button> resolves to the button.
  return !!(el && typeof el.closest === 'function' && el.closest(INTERACTIVE_SELECTOR));
}

export function cardRegionClick(openModal: () => void) {
  return (e: React.MouseEvent): void => {
    if (isInteractiveActivationTarget(e.target)) return;
    openModal();
  };
}
