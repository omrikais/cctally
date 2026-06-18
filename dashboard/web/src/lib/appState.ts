import type { Envelope } from '../types/envelope';

// Loading is GLOBAL — every panel shares one envelope, so env === null means
// the whole dashboard is pre-first-tick. `disconnected` is an OVERLAY on the
// `ready` state (handled in App.tsx), not a tri-state input here.
//
// Truth table (#207 B2/B3):
//   env != null               -> 'ready'   (disconnect is an overlay in App)
//   env == null && error      -> 'error'   (failed bootstrap, no data yet)
//   env == null && !error     -> 'loading' (cold start, skeleton grid)
export type AppState = 'loading' | 'error' | 'ready';

export function deriveAppState(
  env: Envelope | null,
  bootstrapError: boolean,
): AppState {
  if (env != null) return 'ready';
  if (bootstrapError) return 'error';
  return 'loading';
}
