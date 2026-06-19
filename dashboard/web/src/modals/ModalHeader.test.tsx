// Unit coverage for the shared modal-chrome primitives (#210): the
// single-sourced close button (<ModalCloseButton>) and the titled header
// landmark (<ModalHeader>) that the panel <Modal> shell and the
// share-family shells (ShareModal / ComposerModal / ManagePresetsModal)
// all consume. Pinning the glyph + a11y wiring here structurally guards
// the #207 D6 close-glyph drift class at its single source.
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ModalHeader } from './ModalHeader';
import { ModalCloseButton } from './ModalCloseButton';

describe('ModalCloseButton', () => {
  it('renders the canonical × (U+00D7) glyph, never the bespoke ⤬', () => {
    render(<ModalCloseButton onClose={() => {}} />);
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe('×');
    expect(btn.textContent).not.toBe('⤬'); // pre-D6 bespoke glyph
  });

  it('defaults to aria-label "Close", class "modal-close", and type="button"', () => {
    render(<ModalCloseButton onClose={() => {}} />);
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn).toHaveClass('modal-close');
    expect(btn).toHaveAttribute('type', 'button');
  });

  it('honors a custom label and className (share modal close affordance)', () => {
    render(
      <ModalCloseButton
        onClose={() => {}}
        label="Close share modal"
        className="share-modal-close"
      />,
    );
    const btn = screen.getByRole('button', { name: 'Close share modal' });
    expect(btn).toHaveClass('share-modal-close');
    expect(btn.textContent).toBe('×');
  });

  it('fires onClose on click', () => {
    const onClose = vi.fn();
    render(<ModalCloseButton onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('ModalHeader', () => {
  it('renders a <header> landmark with a titled <h2> carrying the aria-labelledby id', () => {
    render(<ModalHeader title="Compose report" titleId="composer-modal-title" />);
    const h2 = screen.getByRole('heading', { level: 2, name: 'Compose report' });
    expect(h2).toHaveAttribute('id', 'composer-modal-title');
    expect(h2.closest('header')).not.toBeNull();
  });

  it('applies a custom header className', () => {
    render(
      <ModalHeader title="x" titleId="y" className="share-modal-header" />,
    );
    expect(screen.getByRole('heading', { level: 2 }).closest('header'))
      .toHaveClass('share-modal-header');
  });

  it('renders NO close button when onClose is omitted (ShareModal title-only header)', () => {
    render(
      <ModalHeader
        title="Share weekly report"
        titleId="share-modal-title"
        className="share-modal-header"
      />,
    );
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders the shared close button with custom label/class when onClose is provided', () => {
    const onClose = vi.fn();
    render(
      <ModalHeader
        title="Manage presets"
        titleId="share-manage-presets-title"
        className="share-manage-header"
        onClose={onClose}
        closeClassName="share-manage-close"
      />,
    );
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe('×');
    expect(btn).toHaveClass('share-manage-close');
    fireEvent.click(btn);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('orders DOM as title → headerExtras → close (panel Modal ShareIcon slot)', () => {
    render(
      <ModalHeader
        title="Weekly"
        titleId="modal-title"
        headerExtras={<span data-testid="extra" className="share-icon" />}
        onClose={() => {}}
      />,
    );
    const header = screen.getByRole('heading', { level: 2 }).closest('header')!;
    const kids = Array.from(header.children);
    const h2Idx = kids.findIndex((k) => k.tagName === 'H2');
    const extraIdx = kids.findIndex((k) => k.getAttribute('data-testid') === 'extra');
    const closeIdx = kids.findIndex((k) => k.getAttribute('aria-label') === 'Close');
    expect(h2Idx).toBeLessThan(extraIdx);
    expect(extraIdx).toBeLessThan(closeIdx);
  });
});
