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
      // Phase B 3-tier: threshold 90 is in the 90-99 band ⇒ warn.
      expect(t!.classList.contains('toast--alert')).toBe(true);
      expect(t!.classList.contains('toast--severity-warn')).toBe(true);
      expect(t!.classList.contains('toast--severity-critical')).toBe(false);
    });

    it('renders BUDGET chip + "$spent of $budget" body on budget axis (issue #19)', () => {
      render(<Toast />);
      act(() => {
        dispatch({
          type: 'SHOW_ALERT_TOAST',
          alert: {
            id: 'budget:2026-04-27T00:00:00Z:90',
            axis: 'budget',
            threshold: 90,
            crossed_at: '2026-04-29T14:32:11Z',
            alerted_at: '2026-04-29T14:32:11Z',
            context: {
              week_start_at: '2026-04-27T00:00:00Z',
              budget_usd: 300,
              spent_usd: 270,
              consumption_pct: 90,
            },
          },
        });
      });
      const t = transientToast();
      expect(t).not.toBeNull();
      expect(t).toHaveTextContent('BUDGET');
      expect(t).toHaveTextContent('90%');
      expect(t!.querySelector('.chip--budget')).not.toBeNull();
      // "$270.00 of $300.00 budget"
      expect(t).toHaveTextContent('$270.00');
      expect(t).toHaveTextContent('$300.00');
      expect(t).toHaveTextContent('budget');
    });

    it('renders PROJECTED chip + "projected …% of cap" on projected weekly_pct axis (issue #121)', () => {
      render(<Toast />);
      act(() => {
        dispatch({
          type: 'SHOW_ALERT_TOAST',
          alert: {
            id: 'projected:2026-04-27T00:00:00Z:weekly_pct:100',
            axis: 'projected',
            metric: 'weekly_pct',
            threshold: 100,
            crossed_at: '2026-04-29T14:32:11Z',
            alerted_at: '2026-04-29T14:32:11Z',
            context: {
              week_start_at: '2026-04-27T00:00:00Z',
              metric: 'weekly_pct',
              projected_value: 102,
              denominator: 100,
            },
          },
        });
      });
      const t = transientToast();
      expect(t).not.toBeNull();
      expect(t).toHaveTextContent('PROJECTED');
      expect(t!.querySelector('.chip--projected')).not.toBeNull();
      expect(t).toHaveTextContent('projected 102% of cap');
      // Threshold 100 is in the >=100 band ⇒ critical.
      expect(t!.classList.contains('toast--severity-critical')).toBe(true);
    });

    it('renders "projected $312 of $300" on projected budget_usd axis (issue #121)', () => {
      render(<Toast />);
      act(() => {
        dispatch({
          type: 'SHOW_ALERT_TOAST',
          alert: {
            id: 'projected:2026-04-27T00:00:00Z:budget_usd:100',
            axis: 'projected',
            metric: 'budget_usd',
            threshold: 100,
            crossed_at: '2026-04-29T14:32:11Z',
            alerted_at: '2026-04-29T14:32:11Z',
            context: {
              week_start_at: '2026-04-27T00:00:00Z',
              metric: 'budget_usd',
              projected_value: 312,
              denominator: 300,
            },
          },
        });
      });
      const t = transientToast();
      expect(t).not.toBeNull();
      expect(t!.querySelector('.chip--projected')).not.toBeNull();
      expect(t).toHaveTextContent('projected $312 of $300');
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

    it('uses the warn tier in the 90-99 band (threshold 95)', () => {
      render(<Toast />);
      act(() => {
        dispatch({ type: 'SHOW_ALERT_TOAST', alert: mkAlert(95, 'weekly') });
      });
      const t = transientToast();
      // Phase B: 95 is in the 90-99 warn band (no longer "red at >=95").
      expect(t!.classList.contains('toast--severity-warn')).toBe(true);
      expect(t!.classList.contains('toast--severity-critical')).toBe(false);
    });

    it('uses the critical tier at threshold >= 100', () => {
      render(<Toast />);
      act(() => {
        dispatch({ type: 'SHOW_ALERT_TOAST', alert: mkAlert(100, 'weekly') });
      });
      const t = transientToast();
      expect(t!.classList.contains('toast--severity-critical')).toBe(true);
      expect(t!.classList.contains('toast--severity-warn')).toBe(false);
    });

    it('consumes the kernel severity token over the threshold band', () => {
      render(<Toast />);
      act(() => {
        // severity:'critical' on a threshold (90) that would derive 'warn'.
        dispatch({
          type: 'SHOW_ALERT_TOAST',
          alert: { ...mkAlert(90, 'weekly'), severity: 'critical' },
        });
      });
      const t = transientToast();
      expect(t!.classList.contains('toast--severity-critical')).toBe(true);
      expect(t!.classList.contains('toast--severity-warn')).toBe(false);
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
