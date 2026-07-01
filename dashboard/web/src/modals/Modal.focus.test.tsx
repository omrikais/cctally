// Modal.focus — integration test for the #207 A1 focus hook wired into the
// shared <Modal> chrome. Opening a real panel modal (via ModalRoot, the same
// path the app uses) must move keyboard focus inside the dialog card; closing
// it must restore focus to the trigger element that was focused at open time.
//
// Mirrors the harness in modals/ProjectsModal.test.tsx — real store via
// `_resetForTests` + `dispatch`, real keymap via `installGlobalKeydown`. We use
// the `alerts` modal (RecentAlertsModal) because it wraps in <Modal> and renders
// with an empty `alerts` array, so no envelope snapshot is needed.
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, render } from '@testing-library/react';
import { Modal } from './Modal';
import { ModalRoot } from './ModalRoot';
import {
  _resetForTests,
  dispatch,
} from '../store/store';
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

describe('<Modal /> focus management (A1)', () => {
  it('moves focus inside the modal card on open and restores to the trigger on close', () => {
    // A trigger button that holds focus at open time. The hook should capture
    // it as the restore target.
    const trigger = document.createElement('button');
    trigger.id = 'panel-trigger';
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement?.id).toBe('panel-trigger');

    render(<ModalRoot />);

    // Open a real panel modal through the same store action the app dispatches.
    act(() => {
      dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    });

    const card = document.querySelector('.modal-card');
    expect(card).toBeTruthy();
    expect(card!.contains(document.activeElement)).toBe(true);

    // Closing the modal unmounts it; focus must return to the trigger.
    act(() => {
      dispatch({ type: 'CLOSE_MODAL' });
    });
    expect(document.querySelector('.modal-card')).toBeNull();
    expect(document.activeElement?.id).toBe('panel-trigger');
  });

  // SH-2 — on open, focus lands on the dialog heading (#modal-title, which
  // ModalHeader marks tabIndex={-1} so `.focus()` takes) rather than the first
  // header focusable (the Share/close affordances). This is the shared modal
  // grammar entry point for every panel modal that routes through <Modal>.
  it('lands focus on the dialog heading on open', () => {
    render(
      <Modal title="Forecast" accentClass="accent-purple">
        <p>body</p>
      </Modal>,
    );
    const heading = document.getElementById('modal-title');
    expect(heading).toBeTruthy();
    expect(document.activeElement).toBe(heading);
  });
});
