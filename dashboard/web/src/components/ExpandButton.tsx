// ExpandButton (#264 S1, AFFORD-1) — the consistent "open this card's modal"
// affordance rendered in every grid panel's header, distinct from ShareIcon.
//
// A dumb leaf (template = ShareIcon.tsx): it owns no store state. Each caller
// wires `onOpen` to that panel's open handler — the same one its section-open
// uses where it has one: Blocks → openActiveOrNewestBlockModal (its section has
// no onClick, so ⤢ is the only general open path), Sessions →
// openMostRecentSessionModal, Alerts → PANEL_REGISTRY.alerts.openAction, the
// rest → their OPEN_MODAL dispatch. The wiring lives per-callsite (not here) so
// each card keeps a single open handler for both its section and its ⤢.
//
// stopPropagation mirrors ShareIcon: most panels open their modal on any
// in-section click, so without the guard this button would double-fire the
// panel-root click alongside `onOpen`.
import type { MouseEvent } from 'react';

interface Props {
  label: string;
  onOpen: () => void;
  // #265 D — when there's nothing to open (e.g. Blocks on a week with no
  // activity blocks), disable the button so it reads inert instead of
  // clickable-but-dead. Optional + default false, so every other caller is
  // unaffected.
  disabled?: boolean;
}

export function ExpandButton({ label, onOpen, disabled = false }: Props) {
  const handleClick = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    if (disabled) return; // defensive: native `disabled` already blocks clicks
    onOpen();
  };
  const disabledText = `${label}: nothing to open yet`;
  return (
    <button
      type="button"
      className="panel-expand"
      aria-label={disabled ? disabledText : `Open ${label}`}
      title={disabled ? disabledText : 'Expand'}
      onClick={handleClick}
      disabled={disabled}
    >
      <span aria-hidden="true">⤢</span>
    </button>
  );
}
