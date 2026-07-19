import type { ReactNode } from 'react';
import { useCallback, useMemo, useRef, useSyncExternalStore } from 'react';
import { useKeymap } from '../hooks/useKeymap';
import {
  dispatch,
  getState,
  subscribeStore,
  topmostStoreFocusLayer,
  type StoreFocusLayer,
} from '../store/store';
import { useModalFocus } from '../hooks/useModalFocus';
import { useScrollLock } from '../hooks/useScrollLock';
import { ModalHeader } from './ModalHeader';

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
  // S2 #264 — wide two-pane variant for Weekly/Monthly; `min(1040px,94vw)`.
  wide?: boolean;
  dataSource?: 'claude' | 'codex' | 'all';
  onClose?: () => void;
  focusLayer?: Exclude<StoreFocusLayer, null>;
  rootId?: string;
  titleId?: string;
  bodyId?: string;
  rootTestId?: string;
  cardClassName?: string;
  triggerId?: string;
}

// Modal grammar (SH-2, a light documented contract — no reorder of shipped
// modals, they already roughly follow it): a panel modal reads top-to-bottom as
//   answer/verdict chip → hero KPI → primary visual → supporting tables.
// On open, focus lands on the dialog heading (initialFocus: 'heading') so the
// reader starts at the title/answer rather than a header affordance (the Share
// icon / close button). Panel modals only — the share family manages its own
// focus in ShareModalRoot and does not route through this hook.
export function Modal({
  title,
  accentClass,
  children,
  headerExtras,
  wide,
  dataSource,
  onClose,
  focusLayer = 'panel',
  rootId = 'modal-root',
  titleId = 'modal-title',
  bodyId = 'modal-body',
  rootTestId,
  cardClassName,
  triggerId,
}: ModalProps) {
  const close = useCallback(() => {
    if (onClose) onClose();
    else dispatch({ type: 'CLOSE_MODAL' });
  }, [onClose]);
  const bindings = useMemo(
    () => [{
      key: 'Escape',
      scope: 'modal' as const,
      layer: focusLayer === 'source-detail' ? 150 : 100,
      action: close,
      when: () => topmostStoreFocusLayer(getState()) === focusLayer,
    }],
    [close, focusLayer],
  );
  useKeymap(bindings);
  // a11y focus management (#207 A1). Modal only mounts while a panel modal is
  // open, so `active` is always true here. The Tab-trap suspends when a
  // higher store-tracked layer (Share/Composer/Update) opens on top.
  const cardRef = useRef<HTMLDivElement>(null);
  const trapEnabled = useSyncExternalStore(
    subscribeStore,
    () => topmostStoreFocusLayer(getState()) === focusLayer,
  );
  useModalFocus(cardRef, {
    active: true,
    trapEnabled,
    initialFocus: 'heading',
    triggerId,
  });
  // M1-1: lock background page scroll while a panel modal is open. Modal
  // mounts only while open, so the active value is always true here.
  useScrollLock(true);
  return (
    <div id={rootId} {...(rootTestId ? { 'data-testid': rootTestId } : {})}>
      <div className="modal-backdrop" onClick={close} />
      <div
        ref={cardRef}
        className={`modal-card ${accentClass}${wide ? ' modal-wide' : ''}${cardClassName ? ` ${cardClassName}` : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-source={dataSource}
      >
        <ModalHeader
          title={title}
          titleId={titleId}
          headerExtras={headerExtras}
          onClose={close}
        />
        <div id={bodyId} className="modal-body">
          {children}
        </div>
      </div>
    </div>
  );
}
