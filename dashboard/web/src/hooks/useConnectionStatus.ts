import { useSyncExternalStore } from 'react';
import { isDisconnected, subscribeConnectionStatus } from '../store/sse';

export function useConnectionStatus(): { disconnected: boolean } {
  const disconnected = useSyncExternalStore(
    subscribeConnectionStatus,
    () => isDisconnected(),
  );
  return { disconnected };
}
