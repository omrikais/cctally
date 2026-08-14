// #293 S4 MODAL-1 — the decorative `.modal-handle` (a fake swipe-to-dismiss
// pill with no gesture wired) is removed. The real dismissal paths — Esc,
// backdrop tap, and the × Close button — are unchanged. Renders a real panel
// modal through ModalRoot (the alerts modal wraps <Modal>), the same path the
// app uses. Non-vacuous: with the handle still rendered, the "no .modal-handle"
// case is RED.
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
import { Modal } from './Modal';
import { ModalRoot } from './ModalRoot';
import { _resetForTests, dispatch } from '../store/store';
import {
  installGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  document.body.innerHTML = '';
});

afterEach(() => {
  _resetKeymap();
});

function openAlertsModal() {
  render(<ModalRoot />);
  act(() => {
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
  });
}

describe('<Modal /> — MODAL-1 fake handle removed (#293 S4)', () => {
  it('renders no .modal-handle element', () => {
    openAlertsModal();
    expect(document.querySelector('.modal-card')).toBeTruthy();
    expect(document.querySelector('.modal-handle')).toBeNull();
  });

  it('Esc still dismisses the modal', () => {
    openAlertsModal();
    expect(document.querySelector('.modal-card')).toBeTruthy();
    act(() => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(document.querySelector('.modal-card')).toBeNull();
  });

  it('backdrop click still dismisses the modal', () => {
    openAlertsModal();
    act(() => {
      fireEvent.click(document.querySelector('.modal-backdrop')!);
    });
    expect(document.querySelector('.modal-card')).toBeNull();
  });

  it('the × Close button still dismisses the modal', () => {
    openAlertsModal();
    act(() => {
      fireEvent.click(document.querySelector('.modal-close')!);
    });
    expect(document.querySelector('.modal-card')).toBeNull();
  });
});

// #556 S4 — `.modal-wide` used to carry two unrelated meanings: the modal's
// width, and whether its body delegates scrolling to internal panes. The All
// Current Usage and All Trend modals need the first without the second, and
// before `paneScroll` they could not ask for one alone: they inherited an
// `overflow: hidden` body with no `.period-two-pane` to scroll, which clipped
// their content with no scroller anywhere at >=1025px.
//
// F7 also deleted the `dataSource` prop here, on the grounds that nothing read
// it. `e2e/period-native-vocabulary.spec.ts` read it, and Playwright runs in a
// different CI job from vitest, so that lane went red on main while these unit
// tests stayed green. The prop is restored, and asserted below in the direction
// that would have caught its removal.
//
// Renders <Modal> directly, following the pattern Modal.focus.test.tsx already
// uses for prop-level assertions; the file's own `openAlertsModal` helper
// cannot parameterize `wide`/`paneScroll`.
describe('<Modal /> — width is separate from the pane-scroll contract (#556 S4)', () => {
  function renderModal(props: { wide?: boolean; paneScroll?: boolean } = {}) {
    return render(
      <Modal title="t" accentClass="accent-blue" {...props}>
        body
      </Modal>,
    );
  }

  it('adds modal-pane-scroll only when paneScroll is set', () => {
    const { container, rerender } = renderModal({ wide: true });
    const card = container.querySelector('.modal-card')!;
    expect(card.className).toContain('modal-wide');
    expect(card.className).not.toContain('modal-pane-scroll');

    rerender(
      <Modal title="t" accentClass="accent-blue" wide paneScroll>
        body
      </Modal>,
    );
    const card2 = container.querySelector('.modal-card')!;
    expect(card2.className).toContain('modal-wide');
    expect(card2.className).toContain('modal-pane-scroll');
  });

  it('publishes dataSource on the modal card, and omits the attribute without it', () => {
    const { container, rerender } = renderModal({ wide: true });
    expect(container.querySelector('.modal-card')!.hasAttribute('data-source')).toBe(false);

    rerender(
      <Modal title="t" accentClass="accent-blue" wide dataSource="codex">
        body
      </Modal>,
    );
    expect(container.querySelector('.modal-card')).toHaveAttribute('data-source', 'codex');
  });
});
