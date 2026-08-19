import { useSyncExternalStore } from 'react';
import {
  connectionState, isDisconnected, isBootstrapError, bootstrapErrorMessage,
  subscribeConnectionStatus, type ConnectionState,
} from '../store/sse';

export function useConnectionStatus(): {
  state: ConnectionState;
  disconnected: boolean;
  bootstrapError: boolean;
  bootstrapMessage: string | null;
} {
  const state = useSyncExternalStore(subscribeConnectionStatus, () => connectionState());
  const disconnected = useSyncExternalStore(subscribeConnectionStatus, () => isDisconnected());
  const bootstrapError = useSyncExternalStore(subscribeConnectionStatus, () => isBootstrapError());
  // #583 S3 §7: null for the ordinary stream error, whose wording the banner
  // already owns; a specific sentence when the raiser knows more — today, the
  // first-frame watchdog, which must say that the STREAM went silent rather
  // than let the banner blame a server that is answering.
  const bootstrapMessage = useSyncExternalStore(
    subscribeConnectionStatus, () => bootstrapErrorMessage(),
  );
  return { state, disconnected, bootstrapError, bootstrapMessage };
}
