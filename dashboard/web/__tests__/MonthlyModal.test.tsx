import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MonthlyModal } from '../src/modals/MonthlyModal';
import { updateSnapshot, dispatch, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<MonthlyModal />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'OPEN_MODAL', kind: 'monthly' });
  });

  it('renders the modal with a pink accent', () => {
    render(<MonthlyModal />);
    const card = document.querySelector('.modal-card');
    expect(card?.classList.contains('accent-pink')).toBe(true);
  });

  it('renders the title with "Monthly history · last 12"', () => {
    render(<MonthlyModal />);
    expect(screen.getByText(/monthly history · last 12/i)).toBeInTheDocument();
  });

  it('does NOT render the subscription-window line', () => {
    render(<MonthlyModal />);
    const win = document.querySelector('.detail-card .window');
    expect(win).toBeNull();
  });

  it('does NOT render the Used % / $/1% stats row', () => {
    render(<MonthlyModal />);
    const stats = document.querySelector('.detail-card .stats2');
    expect(stats).toBeNull();
  });

  it('does NOT render Used% / $/1% columns in the table', () => {
    render(<MonthlyModal />);
    const headers = Array.from(document.querySelectorAll('.history-table thead th'))
      .map((th) => th.textContent ?? '');
    expect(headers.some((h) => /used/i.test(h))).toBe(false);
    expect(headers.some((h) => /\$\/1%/.test(h))).toBe(false);
  });
});
