import { useSyncExternalStore } from 'react';

const QUERY = '(prefers-reduced-motion: reduce)';

// Subscribes to OS-level prefers-reduced-motion preference. Re-renders
// callers when the user toggles the setting at runtime so transitions can
// be suppressed live, not just at mount.
export function useReducedMotion(): boolean {
  return useSyncExternalStore(
    (cb) => {
      if (typeof window === 'undefined' || !window.matchMedia) return () => {};
      const mq = window.matchMedia(QUERY);
      mq.addEventListener('change', cb);
      return () => mq.removeEventListener('change', cb);
    },
    () => {
      if (typeof window === 'undefined' || !window.matchMedia) return false;
      return window.matchMedia(QUERY).matches;
    },
    () => false,
  );
}
