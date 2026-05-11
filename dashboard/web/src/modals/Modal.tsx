import type { ReactNode } from 'react';
import { useMemo } from 'react';
import { useKeymap } from '../hooks/useKeymap';
import { dispatch } from '../store/store';

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
  return (
    <div id="modal-root">
      <div className="modal-backdrop" onClick={close} />
      <div
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
