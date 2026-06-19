import type { ReactNode } from 'react';
import { ModalCloseButton } from './ModalCloseButton';

// Shared modal header chrome (#210; follow-up to the #207 D6 close-glyph
// unification): the `<header>` landmark + titled `<h2>` (carrying the
// `aria-labelledby` target id) + an optional header-extras slot + the
// shared close button. Consumed by the panel <Modal> shell and the
// share-family shells so header markup, the close glyph, and the a11y
// wiring live in one place.
//
// `className` / `closeClassName` keep each shell's existing CSS hooks so
// no stylesheet changes are needed. `onClose` is optional: ShareModal
// renders its close button LAST in the DOM (CSS-positioned into the
// header slot for spec §12.2 tab order), so it passes a title-only header
// here and renders <ModalCloseButton> separately at the card tail. The
// close button, when present, is always last in the header (after the
// extras slot), matching the panel <Modal>'s prior layout where
// `.modal-header .share-icon { margin-left: auto }` floats the ShareIcon.

export interface ModalHeaderProps {
  title: ReactNode;
  /** id placed on the <h2>; the dialog references it via aria-labelledby. */
  titleId?: string;
  /** Header CSS hook. Defaults to the shared "modal-header". */
  className?: string;
  /** Slot rendered between the title and the close button (e.g. a ShareIcon). */
  headerExtras?: ReactNode;
  /** When provided, renders the shared close button inside the header. */
  onClose?: () => void;
  /** aria-label forwarded to the close button (defaults to "Close"). */
  closeLabel?: string;
  /** CSS hook forwarded to the close button (defaults to "modal-close"). */
  closeClassName?: string;
}

export function ModalHeader({
  title,
  titleId,
  className = 'modal-header',
  headerExtras,
  onClose,
  closeLabel,
  closeClassName,
}: ModalHeaderProps) {
  return (
    <header className={className}>
      <h2 id={titleId}>{title}</h2>
      {headerExtras}
      {onClose ? (
        <ModalCloseButton
          onClose={onClose}
          label={closeLabel}
          className={closeClassName}
        />
      ) : null}
    </header>
  );
}
