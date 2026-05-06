import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import type { Envelope } from '../types/envelope';

export function useSnapshot(): Envelope | null {
  return useSyncExternalStore(
    subscribeStore,
    () => getState().snapshot,
  );
}
