import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DailyModal } from '../src/modals/DailyModal';
import { updateSnapshot, dispatch, _resetForTests } from '../src/store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../src/store/keymap';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

// The shared fixture has 6 daily rows (newest-first):
//   2026-04-26 (today, $8.40, cache_hit_pct=87.3)
//   2026-04-25 ($5.71, cache_hit_pct=null)
//   2026-04-24 ($7.06)
//   2026-04-23 ($3.12)
//   2026-04-22 ($7.40)
//   2026-04-21 ($0.00 — disabled bar)

describe('<DailyModal />', () => {
  beforeEach(() => {
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
    updateSnapshot(fixture as unknown as Envelope);
  });

  afterEach(() => {
    uninstallGlobalKeydown();
    document.body.innerHTML = '';
  });

  it('renders the modal with indigo accent and the right title', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    render(<DailyModal />);
    const card = document.querySelector('.modal-card');
    expect(card?.classList.contains('accent-indigo')).toBe(true);
    expect(screen.getByText(/daily history · last 30/i)).toBeInTheDocument();
  });

  it('mount with openDailyDate set: shows that day in the detail card', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2026-04-22' });
    render(<DailyModal />);
    expect(screen.getByText(/^04-22$/)).toBeInTheDocument();
  });

  it('mount without openDailyDate: defaults to today (rows[0])', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    render(<DailyModal />);
    expect(screen.getByText(/^04-26$/)).toBeInTheDocument();
    expect(screen.getByText('Today')).toBeInTheDocument();
  });

  it('mount with stale openDailyDate not in rows: snaps to today silently', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2025-01-01' });
    render(<DailyModal />);
    expect(screen.getByText(/^04-26$/)).toBeInTheDocument();
  });

  it('clicking a non-zero bar re-selects that day', async () => {
    const user = userEvent.setup();
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    render(<DailyModal />);
    const bar = document.querySelector('[data-date="2026-04-23"]') as HTMLButtonElement;
    await user.click(bar);
    expect(screen.getByText(/^04-23$/)).toBeInTheDocument();
  });

  it('ArrowDown moves selection one day older', async () => {
    const user = userEvent.setup();
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    render(<DailyModal />);
    await user.keyboard('{ArrowDown}');
    expect(screen.getByText(/^04-25$/)).toBeInTheDocument();
  });

  it('ArrowUp moves selection one day newer', async () => {
    const user = userEvent.setup();
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2026-04-23' });
    render(<DailyModal />);
    await user.keyboard('{ArrowUp}');
    expect(screen.getByText(/^04-24$/)).toBeInTheDocument();
  });

  it('ArrowDown clamps at the oldest row', async () => {
    const user = userEvent.setup();
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2026-04-21' });
    render(<DailyModal />);
    await user.keyboard('{ArrowDown}');
    expect(screen.getByText(/^04-21$/)).toBeInTheDocument();
  });

  it('ArrowUp clamps at the newest row', async () => {
    const user = userEvent.setup();
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    render(<DailyModal />);
    await user.keyboard('{ArrowUp}');
    expect(screen.getByText(/^04-26$/)).toBeInTheDocument();
  });

  it('empty rows[]: renders the "No usage history yet." placeholder', () => {
    _resetForTests();
    const empty = JSON.parse(JSON.stringify(fixture));
    empty.daily.rows = [];
    empty.daily.peak = null;
    empty.daily.quantile_thresholds = [];
    updateSnapshot(empty as Envelope);
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    render(<DailyModal />);
    expect(screen.getByText(/no usage history yet/i)).toBeInTheDocument();
    expect(document.querySelector('.daily-modal-bars-grid')).toBeNull();
    expect(document.querySelector('.detail-card')).toBeNull();
  });

  it('SSE tick updating today re-renders detail without losing selection', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2026-04-22' });
    render(<DailyModal />);
    const next = JSON.parse(JSON.stringify(fixture));
    next.daily.rows[0].cost_usd = 9.99;
    next.generated_at = '2026-04-26T13:00:00Z';
    act(() => {
      updateSnapshot(next as Envelope);
    });
    expect(screen.getByText(/^04-22$/)).toBeInTheDocument();
  });

  it('cache_hit_pct null on selected day: no cache tile rendered', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2026-04-25' });
    render(<DailyModal />);
    expect(document.querySelector('.tokens-row .t.cache')).toBeNull();
  });
});
