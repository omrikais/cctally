// Single source of the modal close affordance — the `×` (U+00D7) glyph,
// `type="button"`, and the click→close wiring shared by the panel
// <Modal> shell and the share-family shells (ShareModal / ComposerModal /
// ManagePresetsModal). Centralizing the glyph here structurally prevents
// the #207 D6 drift class (a bespoke `⤬` U+292C diverging from the
// canonical `×`): there is now exactly one place the glyph is written.
//
// `label` becomes the button's aria-label (default "Close"); share modals
// pass the more specific "Close share modal". `className` lets each shell
// keep its own CSS hook (`modal-close` / `share-modal-close` /
// `composer-modal-close` / `share-manage-close`) so layout/positioning is
// untouched — ShareModal in particular CSS-absolute-positions its close
// into the header slot and renders it LAST in the DOM for spec §12.2 tab
// order, which is why this is a standalone button rather than something
// the header always owns.

export interface ModalCloseButtonProps {
  onClose: () => void;
  /** aria-label for the button. Defaults to "Close". */
  label?: string;
  /** CSS hook. Defaults to the shared "modal-close". */
  className?: string;
}

export function ModalCloseButton({
  onClose,
  label = 'Close',
  className = 'modal-close',
}: ModalCloseButtonProps) {
  return (
    <button
      type="button"
      className={className}
      aria-label={label}
      onClick={onClose}
    >
      ×
    </button>
  );
}
