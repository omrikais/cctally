import { describe, expect, it, beforeEach } from 'vitest';
import { dispatch, getState, _resetForTests } from '../src/store/store';

const PREFS_KEY = 'ccusage.dashboard.prefs';

describe('store: mobileOnboardingToastSeen', () => {
  beforeEach(() => {
    localStorage.clear();
    // Re-load store state from a clean localStorage. RESET_PREFS won't
    // do this on its own — it deliberately preserves both onboarding
    // flags (per the contract under test) so couldn't reset them.
    _resetForTests();
  });

  it('initializes mobileOnboardingToastSeen to false', () => {
    expect(getState().prefs.mobileOnboardingToastSeen).toBe(false);
  });

  it('MARK_MOBILE_ONBOARDING_TOAST_SEEN sets it to true and persists', () => {
    dispatch({ type: 'MARK_MOBILE_ONBOARDING_TOAST_SEEN' });
    expect(getState().prefs.mobileOnboardingToastSeen).toBe(true);
    const persisted = JSON.parse(localStorage.getItem(PREFS_KEY) ?? '{}');
    expect(persisted.mobileOnboardingToastSeen).toBe(true);
  });

  it('is preserved across RESET_PREFS (forward-only, like the desktop flag)', () => {
    dispatch({ type: 'MARK_MOBILE_ONBOARDING_TOAST_SEEN' });
    dispatch({ type: 'RESET_PREFS' });
    expect(getState().prefs.mobileOnboardingToastSeen).toBe(true);
  });

  it('is independent of onboardingToastSeen', () => {
    dispatch({ type: 'MARK_ONBOARDING_TOAST_SEEN' });
    expect(getState().prefs.onboardingToastSeen).toBe(true);
    expect(getState().prefs.mobileOnboardingToastSeen).toBe(false);
  });
});
