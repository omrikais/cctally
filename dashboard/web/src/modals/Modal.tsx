import type { ReactNode } from 'react';
import { useMemo } from 'react';
import { useKeymap } from '../hooks/useKeymap';
import { dispatch } from '../store/store';

interface ModalProps {
  title: string;
  accentClass: string; // e.g. 'accent-green' | 'accent-purple' | 'accent-amber' | 'accent-orange'
  children: ReactNode;
}

export function Modal({ title, accentClass, children }: ModalProps) {
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
