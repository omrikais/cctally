import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import {
  OUTLINE_WIDTH_MAX,
  OUTLINE_WIDTH_MIN,
  OUTLINE_WIDTH_STEP,
  clampOutlineWidth,
} from '../store/outlineWidth';

// #217 S3 E6(b) — the outline resize divider. A thin vertical handle BETWEEN the
// reader body and the outline column. Pointer-drag to resize; Arrow keys to
// resize from the keyboard; role="separator" + aria-orientation + a labeled,
// value-bearing a11y surface. It does NOT reuse PanelGrip (that is touch-only
// dnd chrome with no resize semantics).
//
// The outline column is on the RIGHT, so dragging the divider LEFT WIDENS it and
// RIGHT narrows it (width = right-edge − pointerX). Arrow keys follow the same
// physical sense: ArrowLeft widens, ArrowRight narrows. Reduced-motion is
// honored implicitly — the resize itself is an instantaneous layout change with
// no transition; we add no animation, so there is nothing to suppress.
//
// Desktop-breakpoint only: at the mobile cutover the outline rides as a sheet
// (not a column), so the divider is hidden by CSS @media (the JSDOM @media gap
// means this is verified in the Playwright pass). PIXEL drag math is likewise a
// Playwright concern; the unit tests cover the keyboard + persistence + a11y.
export function OutlineResizer() {
  const width = useSyncExternalStore(subscribeStore, () => getState().convOutlineWidth);
  const ref = useRef<HTMLDivElement>(null);
  // Pointer-drag state: the column's right edge x + whether a drag is active.
  // Kept in refs so the move/up listeners don't re-subscribe per render.
  const draggingRef = useRef(false);
  const rightEdgeRef = useRef(0);

  const setWidth = useCallback((px: number) => {
    dispatch({ type: 'SET_CONV_OUTLINE_WIDTH', px: clampOutlineWidth(px) });
  }, []);

  // Keyboard resize. Left/Right step by OUTLINE_WIDTH_STEP; Home/End jump to the
  // band extremes. All other keys pass through.
  //
  // Cross-branch review P3 — each HANDLED key also stopPropagation()s so the
  // document-level global keymap doesn't ALSO fire. Without it, pressing `End`
  // while the resizer is focused resizes the outline AND triggers the global
  // `End` (jump-to-latest) — a double-fire. Unhandled keys fall through the
  // default case untouched and still reach the global keymap.
  const onKeyDown = useCallback((ev: React.KeyboardEvent) => {
    const cur = getState().convOutlineWidth;
    switch (ev.key) {
      case 'ArrowLeft':  ev.preventDefault(); ev.stopPropagation(); setWidth(cur + OUTLINE_WIDTH_STEP); break;
      case 'ArrowRight': ev.preventDefault(); ev.stopPropagation(); setWidth(cur - OUTLINE_WIDTH_STEP); break;
      case 'Home':       ev.preventDefault(); ev.stopPropagation(); setWidth(OUTLINE_WIDTH_MAX); break;   // widest
      case 'End':        ev.preventDefault(); ev.stopPropagation(); setWidth(OUTLINE_WIDTH_MIN); break;   // narrowest
      default: break;
    }
  }, [setWidth]);

  // Pointer-drag. On down we capture the column's right edge (the handle sits at
  // the outline's left border, so the column spans [pointerX, rightEdge]); each
  // move recomputes width = rightEdge − pointerX. Listeners live on window so the
  // drag survives the pointer leaving the thin handle.
  const onPointerDown = useCallback((ev: React.PointerEvent) => {
    const host = ref.current?.parentElement;
    if (!host) return;
    // The outline column is the divider's NEXT sibling; its right edge anchors
    // the width computation.
    const outlineEl = ref.current?.nextElementSibling as HTMLElement | null;
    rightEdgeRef.current = outlineEl
      ? outlineEl.getBoundingClientRect().right
      : host.getBoundingClientRect().right;
    draggingRef.current = true;
    ev.preventDefault();
    try { ref.current?.setPointerCapture(ev.pointerId); } catch { /* jsdom */ }
  }, []);

  useEffect(() => {
    const onMove = (ev: PointerEvent) => {
      if (!draggingRef.current) return;
      setWidth(rightEdgeRef.current - ev.clientX);
    };
    const onUp = () => { draggingRef.current = false; };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
  }, [setWidth]);

  return (
    <div
      ref={ref}
      className="conv-outline-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize outline panel"
      aria-valuemin={OUTLINE_WIDTH_MIN}
      aria-valuemax={OUTLINE_WIDTH_MAX}
      aria-valuenow={width}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
    >
      <span className="conv-outline-resizer-grip" aria-hidden="true" />
    </div>
  );
}
