import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { WeeklyModal } from '../src/modals/WeeklyModal';
import { updateSnapshot, dispatch, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<WeeklyModal />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
  });

  it('renders the modal with a cyan accent', () => {
    render(<WeeklyModal />);
    const card = document.querySelector('.modal-card');
    expect(card?.classList.contains('accent-cyan')).toBe(true);
  });

  it('renders the title with "Weekly history · last 12"', () => {
    render(<WeeklyModal />);
    expect(screen.getByText(/weekly history · last 12/i)).toBeInTheDocument();
  });

  it('renders a detail card with the current row by default', () => {
    render(<WeeklyModal />);
    const detail = document.querySelector('.detail-card');
    expect(detail).not.toBeNull();
    // Fixture's first weekly row label is "04-23".
    expect(detail?.textContent).toMatch(/04-23/);
    expect(detail?.textContent).toMatch(/Now/);
  });

  it('shows the subscription-window line on weekly variant', () => {
    render(<WeeklyModal />);
    const win = document.querySelector('.detail-card .window');
    expect(win?.textContent).toMatch(/UTC/);
  });

  it('renders the Used % and $/1% stats row on weekly variant', () => {
    render(<WeeklyModal />);
    const stats = document.querySelector('.detail-card .stats2');
    expect(stats).not.toBeNull();
    expect(stats?.textContent).toMatch(/Used %/);
    expect(stats?.textContent).toMatch(/\$\/1%/);
  });

  it('clicking a non-selected row updates the detail card', () => {
    render(<WeeklyModal />);
    const rows = document.querySelectorAll('.history-table tbody tr');
    expect(rows.length).toBeGreaterThanOrEqual(2);
    fireEvent.click(rows[1]);
    const detail = document.querySelector('.detail-card');
    // Fixture's second weekly row label is "04-16".
    expect(detail?.textContent).toMatch(/04-16/);
  });

  it('selected row gets a class and ▶ marker', () => {
    render(<WeeklyModal />);
    const selected = document.querySelector('.history-table tbody tr.selected');
    expect(selected).not.toBeNull();
    expect(selected?.textContent).toMatch(/▶/);
  });
});
