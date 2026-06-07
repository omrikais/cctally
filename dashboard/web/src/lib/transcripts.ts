import type { Envelope } from '../types/envelope';

// Single source of truth for the conversation-viewer feature gate (spec §5).
// The envelope's `transcriptsEnabled` is true ONLY when the backend would
// serve the transcript GET routes for THIS request (bind gate AND Host
// allowlist). Absent — an older Python without the feature, or the
// pre-first-envelope bootstrap window — is treated as DISABLED, per the
// `types/envelope.ts` contract ("treat as false"); we fail closed.
//
// Every consumer (the header switcher, the Sessions-row entry button, the
// view shell's disabled banner) MUST route through this so the three surfaces
// can never drift to different nullish readings (they previously used three
// different spellings: `=== false || == null`, `=== false`, and `!== false`).
export function transcriptsEnabled(env: Envelope | null | undefined): boolean {
  return env?.transcriptsEnabled === true;
}
