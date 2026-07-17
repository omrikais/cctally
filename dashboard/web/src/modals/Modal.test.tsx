// #293 S4 MODAL-1 — the decorative `.modal-handle` (a fake swipe-to-dismiss
// pill with no gesture wired) is removed. The real dismissal paths — Esc,
// backdrop tap, and the × Close button — are unchanged. Renders a real panel
// modal through ModalRoot (the alerts modal wraps <Modal>), the same path the
// app uses. Non-vacuous: with the handle still rendered, the "no .modal-handle"
// case is RED.
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
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
