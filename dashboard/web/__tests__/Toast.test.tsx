import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, act } from '@testing-library/react';
import { Toast } from '../src/components/Toast';
import { dispatch, _resetForTests } from '../src/store/store';

// Toast renders from store state and auto-dismisses itself after
// ~2500 ms. Smoke test: SHOW_TOAST shows the message; advancing
// fake timers past the dismiss window hides it.

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

// Toast and OnboardingToast both render `role="status"`. Scope queries to
// `.toast` (transient) vs `.onboarding-toast` to disambiguate.
function transientToast(): HTMLElement | null {
  return document.querySelector('.toast');
}

describe('<Toast />', () => {
  it('renders the message on SHOW_STATUS_TOAST and hides it after the dismiss window', () => {
    render(<Toast />);
    expect(transientToast()).toBeNull();

    act(() => {
      dispatch({ type: 'SHOW_STATUS_TOAST', text: 'hello' });
    });
    const t = transientToast();
    expect(t).not.toBeNull();
    expect(t).toHaveTextContent('hello');
    // className check exercises the CSS contract the legacy .toast
    // rules rely on (bottom-centered, amber border).
    expect(t!.classList.contains('toast')).toBe(true);

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(transientToast()).toBeNull();
  });

  it('HIDE_TOAST before the auto-dismiss removes the toast immediately', () => {
    render(<Toast />);
    act(() => {
      dispatch({ type: 'SHOW_STATUS_TOAST', text: 'temp' });
    });
    expect(transientToast()).toBeInTheDocument();
    act(() => {
      dispatch({ type: 'HIDE_TOAST' });
    });
    expect(transientToast()).toBeNull();
  });

  describe('alert variant', () => {
    function mkAlert(threshold = 90, axis: 'weekly' | 'five_hour' = 'weekly') {
      return {
        id: `${axis}:2026-04-27:${threshold}`,
        axis,
        threshold,
        crossed_at: '2026-04-29T14:32:11Z',
        alerted_at: '2026-04-29T14:32:11Z',
        context: {
          week_start_date: '2026-04-27',
          cumulative_cost_usd: 42.5,
          dollars_per_percent: 0.47,
        },
      } as const;
    }

    it('renders threshold + WEEKLY chip when toast.kind === "alert" (weekly axis)', () => {
      render(<Toast />);
      act(() => {
        dispatch({ type: 'SHOW_ALERT_TOAST', alert: mkAlert(90, 'weekly') });
      });
      const t = transientToast();
      expect(t).not.toBeNull();
      expect(t).toHaveTextContent('90%');
      expect(t).toHaveTextContent('WEEKLY');
      // Severity below 95 is amber.
      expect(t!.classList.contains('toast--alert')).toBe(true);
      expect(t!.classList.contains('toast--severity-amber')).toBe(true);
      expect(t!.classList.contains('toast--severity-red')).toBe(false);
    });

    it('renders 5H-BLOCK chip on five_hour axis', () => {
      render(<Toast />);
      act(() => {
        dispatch({
          type: 'SHOW_ALERT_TOAST',
          alert: {
            id: 'five_hour:1714248000:80',
            axis: 'five_hour',
            threshold: 80,
            crossed_at: '2026-04-29T14:32:11Z',
            alerted_at: '2026-04-29T14:32:11Z',
            context: {
              block_start_at: '2026-04-29T14:00:00Z',
              block_cost_usd: 12.34,
              primary_model: 'claude-opus-4-5',
            },
          },
        });
      });
      const t = transientToast();
      expect(t).toHaveTextContent('5H-BLOCK');
      expect(t).toHaveTextContent('80%');
    });

    it('uses red severity at threshold >= 95', () => {
      render(<Toast />);
      act(() => {
        dispatch({ type: 'SHOW_ALERT_TOAST', alert: mkAlert(95, 'weekly') });
      });
      const t = transientToast();
      expect(t!.classList.contains('toast--severity-red')).toBe(true);
      expect(t!.classList.contains('toast--severity-amber')).toBe(false);
    });

    it('alert toast auto-dismisses after the longer 8s window', () => {
      render(<Toast />);
      act(() => {
        dispatch({ type: 'SHOW_ALERT_TOAST', alert: mkAlert(90, 'weekly') });
      });
      // Status-toast window (3s) is not enough for the alert variant.
      act(() => {
        vi.advanceTimersByTime(3000);
      });
      expect(transientToast()).not.toBeNull();
      // After total 9s the alert is gone.
      act(() => {
        vi.advanceTimersByTime(6000);
      });
      expect(transientToast()).toBeNull();
    });

    it('click on alert toast dismisses immediately', () => {
      render(<Toast />);
      act(() => {
        dispatch({ type: 'SHOW_ALERT_TOAST', alert: mkAlert(90, 'weekly') });
      });
      const t = transientToast();
      expect(t).not.toBeNull();
      act(() => {
        t!.click();
      });
      expect(transientToast()).toBeNull();
    });
  });
});
