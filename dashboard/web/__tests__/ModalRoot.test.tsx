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

  it('routes openModal=daily/weekly/monthly to the split period modals, with no History toggle (#264 S2)', () => {
    // #264 S2 un-merged the S8 History modal into three peer modals. Each kind
    // renders a modal card, and the former Day·Week·Month radiogroup toggle is
    // gone entirely (proving the toggle was removed, not just hidden).
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    const { rerender } = render(<ModalRoot />);
    expect(document.querySelector('.modal-card')).toBeTruthy();
    expect(screen.queryByRole('radiogroup')).toBeNull();

    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
    rerender(<ModalRoot />);
    expect(document.querySelector('.modal-card')).toBeTruthy();
    expect(screen.queryByRole('radiogroup')).toBeNull();

    dispatch({ type: 'OPEN_MODAL', kind: 'monthly' });
    rerender(<ModalRoot />);
    expect(document.querySelector('.modal-card')).toBeTruthy();
  });
});
