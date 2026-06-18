import { useSyncExternalStore } from 'react';
import { isDisconnected, isBootstrapError, subscribeConnectionStatus } from '../store/sse';

export function useConnectionStatus(): { disconnected: boolean; bootstrapError: boolean } {
  const disconnected = useSyncExternalStore(subscribeConnectionStatus, () => isDisconnected());
  const bootstrapError = useSyncExternalStore(subscribeConnectionStatus, () => isBootstrapError());
  return { disconnected, bootstrapError };
}
