import type { ReactNode } from 'react';
import { useMemo, useRef, useSyncExternalStore } from 'react';
import { useKeymap } from '../hooks/useKeymap';
import {
  dispatch,
  getState,
  subscribeStore,
  topmostStoreFocusLayer,
} from '../store/store';
import { useModalFocus } from '../hooks/useModalFocus';

interface ModalProps {
  title: string;
  accentClass: string; // e.g. 'accent-green' | 'accent-purple' | 'accent-amber' | 'accent-orange'
  children: ReactNode;
  // Optional slot rendered in the modal header, BEFORE the close button.
  // Used by share-capable modals (plan §M1.10) to inject a <ShareIcon>
  // alongside the existing × close affordance. Kept as a generic
  // ReactNode slot rather than a typed ShareIcon prop so future header
  // chrome (e.g. a basket-add toggle in M3) can use the same slot
  // without churning Modal's signature.
  headerExtras?: ReactNode;
}

export function Modal({ title, accentClass, children, headerExtras }: ModalProps) {
  const close = () => dispatch({ type: 'CLOSE_MODAL' });
  const bindings = useMemo(
    () => [{ key: 'Escape', scope: 'modal' as const, action: close }],
    [],
  );
  useKeymap(bindings);
  // a11y focus management (#207 A1). Modal only mounts while a panel modal is
  // open, so `active` is always true here. The Tab-trap suspends when a
  // higher store-tracked layer (Share/Composer/Update) opens on top.
  const cardRef = useRef<HTMLDivElement>(null);
  const trapEnabled = useSyncExternalStore(
    subscribeStore,
    () => topmostStoreFocusLayer(getState()) === 'panel',
  );
  useModalFocus(cardRef, { active: true, trapEnabled });
  return (
    <div id="modal-root">
      <div className="modal-backdrop" onClick={close} />
      <div
        ref={cardRef}
        className={`modal-card ${accentClass}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
      >
        {/* Decorative drag-handle. Visible only at the mobile breakpoint
            via CSS. There is no swipe-to-dismiss gesture wired up; the
            handle sets the visual expectation only. Dismissal paths
            remain X-button, backdrop tap, and Esc. */}
        <div className="modal-handle" aria-hidden="true" />
        <header className="modal-header">
          <h2 id="modal-title">{title}</h2>
          {headerExtras}
          <button className="modal-close" aria-label="Close" onClick={close}>
            ×
          </button>
        </header>
        <div id="modal-body" className="modal-body">
          {children}
        </div>
      </div>
    </div>
  );
}
