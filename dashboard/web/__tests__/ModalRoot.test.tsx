import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModalRoot } from '../src/modals/ModalRoot';
import { dispatch, updateSnapshot, _resetForTests, getState } from '../src/store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../src/store/keymap';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<ModalRoot />', () => {
  beforeEach(() => {
    _resetForTests();
    _resetKeymap();
    updateSnapshot(fixture as unknown as Envelope);
    installGlobalKeydown();
  });

  it('renders nothing when no modal is open', () => {
    render(<ModalRoot />);
    expect(document.querySelector('.modal-card')).toBeNull();
  });

  it('renders CurrentWeekModal when openModal is current-week', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    render(<ModalRoot />);
    expect(document.querySelector('.modal-card')).toBeTruthy();
  });

  it('Escape closes the modal', async () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    render(<ModalRoot />);
    const user = userEvent.setup();
    await user.keyboard('{Escape}');
    expect(getState().openModal).toBe(null);
    uninstallGlobalKeydown();
  });

  it('renders the HistoryModal when openModal=history (S8 #254 — one modal replaces Daily/Weekly/Monthly)', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'history' });
    render(<ModalRoot />);
    // The consolidated History modal always renders its Day·Week·Month
    // radiogroup toggle, whatever period/dataset is active.
    expect(screen.getByRole('radiogroup', { name: /history period/i })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Day' })).toBeInTheDocument();
  });
});
