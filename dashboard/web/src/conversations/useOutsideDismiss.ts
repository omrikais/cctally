import { useEffect, useRef } from 'react';

// #238 R3 — dismiss an open popover when a pointerdown lands outside `ref`.
// Shared by the reader-header menus. The dismiss MUST be silent (setOpen(false)),
// never the focus-restoring close(): pointerdown fires before the clicked-outside
// control is focused, so refocusing the trigger would steal focus back (churn).
// pointerdown (not click) closes before focus/selection side-effects. Capture
// phase so a child stopPropagation can't suppress the outside check.
export function useOutsideDismiss(
  ref: React.RefObject<HTMLElement | null>,
  open: boolean,
  onDismiss: () => void,
): void {
  const cb = useRef(onDismiss);
  cb.current = onDismiss;
  useEffect(() => {
    if (!open || typeof document === 'undefined') return;
    const onPointerDown = (e: PointerEvent) => {
      const el = ref.current;
      if (el && !el.contains(e.target as Node | null)) cb.current();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [open, ref]);
}
