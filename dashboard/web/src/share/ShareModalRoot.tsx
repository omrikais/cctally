// Top-level overlay mounter for the share-modal layer (spec §6.1, plan
// §M1.11).
//
// This sits ALONGSIDE the existing <ModalRoot> in App.tsx and renders
// independently — opening the share modal does NOT close any underlying
// panel modal. Its z-index is higher than .modal-backdrop / .modal-card
// so it visually layers above the panel modal.
//
// Focus restoration (spec §12.8): when the share modal opens we capture
// the active element (the ShareIcon that fired the dispatch) into a ref.
// On close we re-focus that element. If the modal was opened via the
// `triggerId` slot (set by `dispatch(openShareModal(panel, triggerId))`)
// we prefer `document.getElementById(triggerId)?.focus()` since the
// `document.activeElement` capture racing with click → blur is flaky.
//
// Lazy-mount invariant: <ShareModal> is mounted ONLY when
// `state.shareModal !== null`. That keeps the dashboard's cold-start
// import graph free of share-modal child components (TemplateGallery,
// Knobs, PreviewPane, ActionBar) until the user actually clicks a
// ShareIcon — matches the spec's "minimal cold-start cost" guidance.
import { useEffect, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { closeShareModal } from '../store/shareSlice';
import { ShareModal } from './ShareModal';

export function ShareModalRoot() {
  const slot = useSyncExternalStore(subscribeStore, () => getState().shareModal);
  // Capture the element that opened the modal on FIRST mount of an open
  // slot; restore focus to it on close. Tracked across the slot's life so
  // an in-modal re-render does not re-capture.
  const triggerElementRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);

  useEffect(() => {
    if (slot) {
      if (!wasOpenRef.current) {
        wasOpenRef.current = true;
        // Prefer the id passed via dispatch (stable, survives the
        // ShareIcon click → activeElement-blur race in JSDOM and some
        // browsers). Fall back to whatever currently has focus.
        const byId = slot.triggerId
          ? document.getElementById(slot.triggerId)
          : null;
        triggerElementRef.current =
          (byId as HTMLElement | null) ??
          (document.activeElement as HTMLElement | null);
      }
    } else if (wasOpenRef.current) {
      // Just closed. Restore focus to the captured trigger, then clear
      // the ref. Guard the call — the element may have unmounted (e.g.
      // panel order changed while the modal was open).
      wasOpenRef.current = false;
      const el = triggerElementRef.current;
      triggerElementRef.current = null;
      if (el && typeof el.focus === 'function' && document.contains(el)) {
        el.focus();
      }
    }
  }, [slot]);

  if (!slot) return null;

  const close = () => dispatch(closeShareModal());
  return (
    <div
      id="share-modal-root"
      className="share-overlay"
      // Click-outside (on the backdrop, not on the modal card itself) closes
      // the modal. The card's own click handler stops propagation so clicks
      // inside the card don't bubble back here. stopPropagation() on the
      // overlay itself prevents any of those clicks from reaching the
      // underlying panel/modal (we don't want to accidentally swap which
      // panel modal is open while the share modal is up).
      onClick={(e) => {
        e.stopPropagation();
        if (e.target === e.currentTarget) close();
      }}
    >
      <ShareModal panel={slot.panel} onClose={close} />
    </div>
  );
}
