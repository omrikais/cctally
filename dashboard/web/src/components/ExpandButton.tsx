// ExpandButton (#264 S1, AFFORD-1) — the consistent "open this card's modal"
// affordance rendered in every grid panel's header, distinct from ShareIcon.
//
// A dumb leaf (template = ShareIcon.tsx): it owns no store state. The caller
// wires `onOpen` to the SAME open handler that panel's section-open already
// uses (per-panel; e.g. Blocks → openActiveOrNewestBlockModal, Sessions →
// openMostRecentSessionModal, the rest → their OPEN_MODAL dispatch) — panels
// can't import panelRegistry back, so the wiring lives at each callsite.
//
// stopPropagation mirrors ShareIcon: most panels open their modal on any
// in-section click, so without the guard this button would double-fire the
// panel-root click alongside `onOpen`.
import type { MouseEvent } from 'react';

interface Props {
  label: string;
  onOpen: () => void;
}

export function ExpandButton({ label, onOpen }: Props) {
  const handleClick = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    onOpen();
  };
  return (
    <button
      type="button"
      className="panel-expand"
      aria-label={`Open ${label}`}
      title="Expand"
      onClick={handleClick}
    >
      <span aria-hidden="true">⤢</span>
    </button>
  );
}
