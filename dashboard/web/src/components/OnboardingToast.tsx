import { useEffect, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useIsMobile } from '../hooks/useIsMobile';

const AUTO_DISMISS_MS = 8000;

const DESKTOP_COPY = 'New: hold any card to rearrange the dashboard.';
const MOBILE_COPY  = 'Tap to drill in · long-press to rearrange · ⟳ to refresh';

export function OnboardingToast() {
  const isMobile = useIsMobile();
  const desktopSeen = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.onboardingToastSeen,
  );
  const mobileSeen = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.mobileOnboardingToastSeen,
  );

  const seen = isMobile ? mobileSeen : desktopSeen;
  const action = isMobile
    ? ('MARK_MOBILE_ONBOARDING_TOAST_SEEN' as const)
    : ('MARK_ONBOARDING_TOAST_SEEN' as const);
  const msg = isMobile ? MOBILE_COPY : DESKTOP_COPY;

  useEffect(() => {
    if (seen) return;
    const t = setTimeout(() => dispatch({ type: action }), AUTO_DISMISS_MS);
    return () => clearTimeout(t);
  }, [seen, action]);

  if (seen) return null;

  return (
    <div className="onboarding-toast" role="status" aria-live="polite">
      <span className="onboarding-toast-msg">{msg}</span>
      <button
        className="onboarding-toast-dismiss"
        type="button"
        aria-label="Dismiss"
        onClick={() => dispatch({ type: action })}
      >
        ×
      </button>
    </div>
  );
}
