import { render, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Toast } from './Toast';
import { _resetForTests, dispatch } from '../store/store';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  // useIsMobile (inside OnboardingToast) reads matchMedia; default desktop.
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {},
    dispatchEvent: () => false,
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('<Toast /> onboarding suppression (#207 D8)', () => {
  it('renders the onboarding toast visibly when no status toast is live', () => {
    const { container } = render(<Toast />);
    const ob = container.querySelector('.onboarding-toast') as HTMLElement | null;
    expect(ob).not.toBeNull();
    expect(ob!.hidden).toBe(false);
  });

  it('hides the onboarding toast while a status toast is live, preserving the node/timer', () => {
    act(() => { dispatch({ type: 'SHOW_STATUS_TOAST', text: 'synced' }); });
    const { container } = render(<Toast />);
    const ob = container.querySelector('.onboarding-toast') as HTMLElement | null;
    // node still mounted (timer intact) but visually hidden:
    expect(ob === null || ob.hidden || ob.style.display === 'none').toBe(true);
    // Specifically: the node IS still mounted and merely hidden, not unmounted.
    expect(ob).not.toBeNull();
    expect(ob!.hidden).toBe(true);
  });
});
