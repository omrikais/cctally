import { useSnapshot } from './useSnapshot';

// DisplayState — the camelCase, browser-facing view of the snapshot
// envelope's `display` block. The server resolves "local" to a concrete
// IANA zone before the envelope leaves Python (per F1 of the
// localize-datetime-display spec); the browser MUST NOT call its own
// Intl resolver. When the snapshot is null or pre-display (legacy
// envelope from an older server), DEFAULT_DISPLAY is returned so
// formatters never crash on a missing block.
export interface DisplayState {
  tz:            string;
  resolvedTz:    string;
  offsetLabel:   string;
  offsetSeconds: number;
  // True when the server was launched with --tz; the persisted
  // display.tz is held read-only for the lifetime of the dashboard
  // process and POST /api/settings is rejected. Defaults to false
  // when the envelope omits the key (legacy / non-pinned).
  pinned:        boolean;
}

const DEFAULT_DISPLAY: DisplayState = {
  tz: 'local',
  resolvedTz: 'Etc/UTC',
  offsetLabel: 'UTC',
  offsetSeconds: 0,
  pinned: false,
};

export function useDisplayTz(): DisplayState {
  const env = useSnapshot();
  if (!env?.display) return DEFAULT_DISPLAY;
  return {
    tz: env.display.tz,
    resolvedTz: env.display.resolved_tz,
    offsetLabel: env.display.offset_label,
    offsetSeconds: env.display.offset_seconds,
    pinned: env.display.pinned ?? false,
  };
}
