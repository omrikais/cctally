// Reusable share-affordance button rendered in panel headers and modal
// headers for every share-capable panel (spec §6.1, plan §M1.9).
//
// The button is intentionally dumb: it owns no store state, no modal
// plumbing. The caller wires `onClick` to `dispatch(openShareModal(...))`
// — keeps the component reusable across panels and modals without
// hardcoding the trigger-id convention.
//
// a11y:
//   - `aria-label="Share <panelLabel> report"` — distinct per panel so
//     screen readers don't read 8 identical "share" buttons.
//   - `title="Share (S)"` — keyboard shortcut lands in M1.11+; the
//     tooltip can precede it since the affordance is discoverable.
//   - `data-share-panel` — CSS / future keyboard-test hook.
//   - `triggerId` (optional) — sets `id=<triggerId>` on the button so
//     `ShareModalRoot`'s focus-restore (spec §12.8) can re-resolve the
//     trigger element via `document.getElementById(triggerId)`. Callers
//     pass the SAME string they pass as the 2nd arg of
//     `dispatch(openShareModal(panel, triggerId))`.
import type { MouseEvent } from 'react';
import type { SharePanelId } from '../share/types';

interface Props {
  panel: SharePanelId;
  panelLabel: string;
  onClick: () => void;
  triggerId?: string;
}

export function ShareIcon({ panel, panelLabel, onClick, triggerId }: Props) {
  // Stop the click from bubbling to the enclosing panel/modal section.
  // Most panels treat any in-section click as "open the panel modal,"
  // so without this guard the share button would also fire the panel
  // modal alongside the dispatch its caller wanted. Mirrors the
  // `panel-collapse-toggle` callsite pattern.
  const handleClick = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    onClick();
  };
  return (
    <button
      type="button"
      className="share-icon"
      data-share-panel={panel}
      aria-label={`Share ${panelLabel} report`}
      onClick={handleClick}
      title="Share (S)"
      {...(triggerId ? { id: triggerId } : {})}
    >
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <path
          d="M3 7h6m0 0L6.5 4.5M9 7L6.5 9.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <rect
          x="0.75"
          y="0.75"
          width="12.5"
          height="12.5"
          rx="1.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1"
        />
      </svg>
    </button>
  );
}
