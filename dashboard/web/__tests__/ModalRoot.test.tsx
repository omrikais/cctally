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

  it('renders WeeklyModal when openModal=weekly', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
    render(<ModalRoot />);
    expect(screen.getByText(/weekly history · last 12/i)).toBeInTheDocument();
  });

  it('renders MonthlyModal when openModal=monthly', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'monthly' });
    render(<ModalRoot />);
    expect(screen.getByText(/monthly history · last 12/i)).toBeInTheDocument();
  });

  it('renders DailyModal when openModal === "daily"', () => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    render(<ModalRoot />);
    expect(screen.getByText(/daily history · last 30/i)).toBeInTheDocument();
  });
});
