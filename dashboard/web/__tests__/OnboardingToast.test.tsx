import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { OnboardingToast } from '../src/components/OnboardingToast';
import { _resetForTests, getState, dispatch } from '../src/store/store';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.useFakeTimers();
});

describe('<OnboardingToast />', () => {
  it('renders when onboardingToastSeen=false', () => {
    render(<OnboardingToast />);
    expect(screen.getByText(/hold any card to rearrange/i)).toBeTruthy();
  });

  it('does NOT render when onboardingToastSeen=true', () => {
    dispatch({ type: 'MARK_ONBOARDING_TOAST_SEEN' });
    const { container } = render(<OnboardingToast />);
    expect(container.textContent).not.toMatch(/hold any card/i);
  });

  it('clicking the close button dispatches MARK_ONBOARDING_TOAST_SEEN', async () => {
    vi.useRealTimers();
    const user = userEvent.setup();
    render(<OnboardingToast />);
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(getState().prefs.onboardingToastSeen).toBe(true);
  });

  it('auto-dismisses after 8 seconds', () => {
    render(<OnboardingToast />);
    expect(getState().prefs.onboardingToastSeen).toBe(false);
    act(() => { vi.advanceTimersByTime(8001); });
    expect(getState().prefs.onboardingToastSeen).toBe(true);
  });
});
