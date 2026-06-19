import { render, screen, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { UpdateSuccessModal } from './UpdateSuccessModal';
import { _resetForTests } from '../store/store';

// The modal polls refreshUpdateState() on an interval; stub it so no network
// is hit and the poll cap can be exercised purely via fake timers.
vi.mock('../store/update', () => ({
  refreshUpdateState: vi.fn().mockResolvedValue(undefined),
}));

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('<UpdateSuccessModal /> bounded reconnect (#207 D7)', () => {
  it('shows the Esc hint while reconnecting', () => {
    render(<UpdateSuccessModal />);
    expect(screen.getByText(/press esc to close/i)).toBeTruthy();
  });

  it('stops polling after the cap and shows the timeout message + Close', async () => {
    render(<UpdateSuccessModal />);
    // Advance well past the cap (20 polls @ 1500ms = 30s); one extra tick to
    // cross the boundary.
    await act(async () => { vi.advanceTimersByTime(1500 * 21 + 1500); });
    expect(screen.getByText(/still reconnecting/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /close/i })).toBeTruthy();
    // The Esc hint is gone in the timed-out state.
    expect(screen.queryByText(/press esc to close/i)).toBeNull();
  });
});
