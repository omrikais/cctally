import { useSyncExternalStore } from 'react';
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { dispatch, getState, subscribeStore } from '../store/store';
import { shouldSuppressNextClick } from '../lib/clickSuppression';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { PANEL_REGISTRY, type PanelId } from '../lib/panelRegistry';

export interface PanelHostProps {
  id: PanelId;
  index: number;
}

export function PanelHost({ id, index }: PanelHostProps) {
  const def = PANEL_REGISTRY[id];

  // Disable drag activation while a modal is open. Subscribed via
  // useSyncExternalStore so the disabled state flips reactively.
  const dragDisabled = useSyncExternalStore(
    subscribeStore,
    () => getState().openModal != null,
  );

  const reducedMotion = useReducedMotion();

  // We deliberately ignore `attributes` from useSortable. dnd-kit puts a
  // `role="button"` + `tabIndex=0` on the wrapper, which would create an
  // extra (inert) tab stop alongside the inner panel's own tabIndex=0.
  // Pointer drag activation lives entirely in `listeners`.
  const {
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id, disabled: dragDisabled });

  // Defensive: if PANEL_REGISTRY is somehow missing this id (stale localStorage
  // racing reconcilePanelOrder, or a future migration mid-deploy), render nothing
  // rather than throwing.
  if (!def) return null;
  const { Component, label } = def;

  // Capture-phase click suppression: when a drag just ended, swallow the
  // synthesized click before it reaches the inner panel's onClick.
  const onClickCapture = (e: React.MouseEvent) => {
    if (shouldSuppressNextClick()) {
      e.stopPropagation();
    }
  };

  // Shift+Arrow keyboard reorder is independent of dnd-kit's drag lifecycle —
  // it dispatches a direct swap. dnd-kit's default keyboard activation
  // (Space/Enter to grab, Arrows to move, Space/Enter to drop) is not bound
  // here; we use this discrete shortcut instead.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!e.shiftKey) return;
    // Bail when the keydown bubbled up from an editable descendant
    // (e.g., the Sessions filter/search input). Shift+Arrow there is
    // text-selection — preventDefault'ing it would break that.
    const target = e.target as HTMLElement;
    if (typeof target.matches === 'function' &&
        target.matches('input, textarea, select, [contenteditable="true"]')) {
      return;
    }
    let direction: -1 | 1 | 0 = 0;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') direction = 1;
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') direction = -1;
    if (direction === 0) return;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    dispatch({ type: 'SWAP_PANELS', index, direction });
  };

  // Inline `transition` from useSortable wins over any CSS rule on the same
  // property, so prefers-reduced-motion must be honored here in JS — a
  // media-query rule in index.css cannot suppress an inline value.
  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition: reducedMotion ? undefined : transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`panel-host${isDragging ? ' is-dragging' : ''}`}
      data-panel-host={id}
      data-panel-index={index}
      aria-label={`Reorder ${label}`}
      onKeyDown={onKeyDown}
      onClickCapture={onClickCapture}
      {...listeners}
    >
      {/* Hover affordance: "hold to rearrange" hint shown via CSS on hover.
         The touch-only drag handle (.panel-grip) is rendered inside each
         panel's own .panel-header so it sits next to the chevron rather
         than overlapping it; see components/PanelGrip.tsx. */}
      <span className="panel-grip-hint" aria-hidden="true">hold to rearrange</span>
      <Component />
    </div>
  );
}
