import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { SyncChip } from '../src/components/SyncChip';
import {
  dispatch,
  updateSnapshot,
  _resetForTests,
} from '../src/store/store';
import type { Envelope } from '../src/types/envelope';

// Regression: the sync-error floor must survive the SyncChip's own
// 1-second setInterval. Previously `sync.ts` wrote `chip.textContent`
// and added `.sync-error` directly; the chip's next tick re-rendered
// "synced Xs ago" within 1 s, wiping the failure message. Now the floor
// lives in store state (syncErrorFloorUntil), so the chip force-renders
// "⚠ sync failed" + `.sync-error` for the full 3 seconds.

function mkEnvelopeWithSyncAge(ageS: number): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-04-24T10:00:00Z',
    last_sync_at: '2026-04-24T09:59:55Z',
    sync_age_s: ageS,
    last_sync_error: null,
    header: {
      week_label: 'Apr 20–27',
      used_pct: 0,
      five_hour_pct: null,
      dollar_per_pct: null,
      forecast_pct: null,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks:  { rows: [] },
    daily:   { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [] },
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('<SyncChip /> error floor', () => {
  it('renders ⚠ sync failed + .sync-error for the full floor duration', () => {
    // Seed a happy-path envelope so the chip would otherwise read
    // "synced Xs ago" and clobber the error message on the next tick.
    updateSnapshot(mkEnvelopeWithSyncAge(5));
    render(<SyncChip />);
    // Pre-floor: env-driven text.
    expect(screen.getByText(/synced \d+s ago/)).toBeInTheDocument();
    const chipEl = screen.getByText(/synced \d+s ago/);
    expect(chipEl.classList.contains('sync-error')).toBe(false);

    // Apply a 3-second floor from "now".
    const nowBefore = Date.now();
    act(() => {
      dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: nowBefore + 3000 });
    });

    // Immediate: floor text + class.
    expect(screen.getByText('⚠ sync failed')).toBeInTheDocument();
    expect(screen.getByText('⚠ sync failed').classList.contains('sync-error')).toBe(true);

    // Advance ~100 ms (less than the chip's 1-second tick) and verify
    // the error text is still there — the key bug the fix closed.
    act(() => { vi.advanceTimersByTime(100); });
    expect(screen.getByText('⚠ sync failed')).toBeInTheDocument();
    expect(screen.getByText('⚠ sync failed').classList.contains('sync-error')).toBe(true);

    // Advance past 1 s (the chip's tick interval). Without the store-
    // mediated floor, this is where the legacy bug restored "synced Xs
    // ago"; assert the floor still holds.
    act(() => { vi.advanceTimersByTime(1500); });
    expect(screen.getByText('⚠ sync failed')).toBeInTheDocument();
    expect(screen.getByText('⚠ sync failed').classList.contains('sync-error')).toBe(true);

    // Advance past the floor expiry (total elapsed > 3 s); the chip
    // should resume env-driven rendering and the class must go away.
    act(() => { vi.advanceTimersByTime(2000); });
    expect(screen.queryByText('⚠ sync failed')).not.toBeInTheDocument();
    const revertedEl = screen.getByText(/synced \d+s ago/);
    expect(revertedEl.classList.contains('sync-error')).toBe(false);
  });

  it('does not paint the floor when syncErrorFloorUntil is in the past', () => {
    updateSnapshot(mkEnvelopeWithSyncAge(1));
    // Apply an already-expired floor (e.g., stale state) and ensure
    // the chip falls through to env-driven rendering immediately.
    act(() => {
      dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: Date.now() - 10 });
    });
    render(<SyncChip />);
    expect(screen.queryByText('⚠ sync failed')).not.toBeInTheDocument();
    expect(screen.getByText(/synced \d+s ago/)).toBeInTheDocument();
  });
});

describe('<SyncChip /> success flash', () => {
  it('renders ✓ updated + .sync-success for the flash duration', () => {
    updateSnapshot(mkEnvelopeWithSyncAge(5));
    render(<SyncChip />);
    expect(screen.getByText(/synced \d+s ago/)).toBeInTheDocument();

    const nowBefore = Date.now();
    act(() => {
      dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: nowBefore + 1200 });
    });

    const flashEl = screen.getByText('✓ updated');
    expect(flashEl).toBeInTheDocument();
    expect(flashEl.classList.contains('sync-success')).toBe(true);

    // Mid-flash: still rendered.
    act(() => { vi.advanceTimersByTime(800); });
    expect(screen.getByText('✓ updated')).toBeInTheDocument();

    // Past expiry: chip falls back to env-driven default.
    act(() => { vi.advanceTimersByTime(500); });
    expect(screen.queryByText('✓ updated')).not.toBeInTheDocument();
    expect(screen.getByText(/synced \d+s ago/)).toBeInTheDocument();
  });

  it('does not paint when syncSuccessFlashUntil is in the past', () => {
    updateSnapshot(mkEnvelopeWithSyncAge(2));
    act(() => {
      dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: Date.now() - 10 });
    });
    render(<SyncChip />);
    expect(screen.queryByText('✓ updated')).not.toBeInTheDocument();
    expect(screen.getByText(/synced \d+s ago/)).toBeInTheDocument();
  });
});

describe('<SyncChip /> render priority', () => {
  // Contract: busy > error-floor > success-flash > default.
  // A click during error-floor is a retry; "syncing…" reflects the new
  // request's progress. Error wins over success when both timer-active.
  it('busy beats error-floor and success-flash', () => {
    updateSnapshot(mkEnvelopeWithSyncAge(3));
    const t = Date.now();
    act(() => {
      dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: t + 3000 });
      dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: t + 1200 });
      dispatch({ type: 'SET_SYNC_BUSY', busy: true });
    });
    render(<SyncChip />);
    expect(screen.getByText('syncing…')).toBeInTheDocument();
    expect(screen.queryByText('⚠ sync failed')).not.toBeInTheDocument();
    expect(screen.queryByText('✓ updated')).not.toBeInTheDocument();
  });

  it('error-floor beats success-flash when busy is false', () => {
    updateSnapshot(mkEnvelopeWithSyncAge(3));
    const t = Date.now();
    act(() => {
      dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: t + 3000 });
      dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: t + 1200 });
    });
    render(<SyncChip />);
    expect(screen.getByText('⚠ sync failed')).toBeInTheDocument();
    expect(screen.queryByText('✓ updated')).not.toBeInTheDocument();
  });

  it('success-flash beats default when no error/busy', () => {
    updateSnapshot(mkEnvelopeWithSyncAge(3));
    act(() => {
      dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: Date.now() + 1200 });
    });
    render(<SyncChip />);
    expect(screen.getByText('✓ updated')).toBeInTheDocument();
    expect(screen.queryByText(/synced \d+s ago/)).not.toBeInTheDocument();
  });
});

describe('<SyncChip /> aria-live', () => {
  it('every render branch carries aria-live="polite"', () => {
    updateSnapshot(mkEnvelopeWithSyncAge(3));
    const { rerender } = render(<SyncChip />);

    // Default branch.
    let chip = document.getElementById('sync-chip')!;
    expect(chip.getAttribute('aria-live')).toBe('polite');

    // Busy branch.
    act(() => { dispatch({ type: 'SET_SYNC_BUSY', busy: true }); });
    rerender(<SyncChip />);
    chip = document.getElementById('sync-chip')!;
    expect(chip.getAttribute('aria-live')).toBe('polite');
    expect(chip.getAttribute('aria-busy')).toBe('true');

    // Error branch.
    act(() => {
      dispatch({ type: 'SET_SYNC_BUSY', busy: false });
      dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: Date.now() + 3000 });
    });
    rerender(<SyncChip />);
    chip = document.getElementById('sync-chip')!;
    expect(chip.getAttribute('aria-live')).toBe('polite');

    // Success branch.
    act(() => {
      dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: 0 });
      dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: Date.now() + 1200 });
    });
    rerender(<SyncChip />);
    chip = document.getElementById('sync-chip')!;
    expect(chip.getAttribute('aria-live')).toBe('polite');
  });
});
