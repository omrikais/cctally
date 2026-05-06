import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { OnboardingToast } from '../src/components/OnboardingToast';
import { dispatch, getState, _resetForTests } from '../src/store/store';
import { MOBILE_MEDIA_QUERY } from '../src/lib/breakpoints';

let mqlMatches = false;

function installMatchMedia() {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: q === MOBILE_MEDIA_QUERY ? mqlMatches : false,
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

describe('OnboardingToast — mobile branch', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    installMatchMedia();
  });

  afterEach(() => {
    cleanup();
  });

  it('renders the mobile gesture-summary copy when useIsMobile && !mobileSeen', () => {
    mqlMatches = true;
    render(<OnboardingToast />);
    expect(screen.getByText(/long-press to rearrange/i)).toBeTruthy();
  });

  it('renders the desktop copy when !useIsMobile && !desktopSeen', () => {
    mqlMatches = false;
    render(<OnboardingToast />);
    expect(screen.getByText(/hold any card to rearrange/i)).toBeTruthy();
  });

  it('mobile dismiss dispatches MARK_MOBILE_ONBOARDING_TOAST_SEEN', () => {
    mqlMatches = true;
    render(<OnboardingToast />);
    fireEvent.click(screen.getByLabelText('Dismiss'));
    expect(getState().prefs.mobileOnboardingToastSeen).toBe(true);
    expect(getState().prefs.onboardingToastSeen).toBe(false);
  });

  it('desktop dismiss dispatches MARK_ONBOARDING_TOAST_SEEN', () => {
    mqlMatches = false;
    render(<OnboardingToast />);
    fireEvent.click(screen.getByLabelText('Dismiss'));
    expect(getState().prefs.onboardingToastSeen).toBe(true);
    expect(getState().prefs.mobileOnboardingToastSeen).toBe(false);
  });

  it('returns null when the matching flag is already set', () => {
    mqlMatches = true;
    dispatch({ type: 'MARK_MOBILE_ONBOARDING_TOAST_SEEN' });
    const { container } = render(<OnboardingToast />);
    expect(container.firstChild).toBeNull();
  });
});
