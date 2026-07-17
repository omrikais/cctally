// #294 S5 — persisted global source selection (Claude / Codex / All).
//
// A tiny localStorage-backed pure-IO module mirroring outlineWidth.ts. The
// store seeds `activeSource` from `loadActiveSource()` and persists via
// `saveActiveSource()` on every SET_ACTIVE_SOURCE that actually changes it.
//
// Bootstrap precedence (§5.1): a stored value that is exactly one of the three
// literals wins; anything else (missing, unknown, malformed) falls back to
// 'claude', which is by construction the wire `default_source` constant — the
// store never waits for an envelope to initialize and never reconciles the
// choice against later envelopes. The value is a BARE literal, not JSON, so a
// "garbage" stored value is simply a string that fails the membership check.

import type { DashboardSelection } from '../types/envelope';

// New surface → the `cctally:<area>:<name>` namespace (matching
// BASKET_STORAGE_KEY's colon form), NOT the legacy `ccusage.*` prefs blob.
export const SOURCE_STORAGE_KEY = 'cctally:dashboard:source';

const VALID_SELECTIONS: ReadonlySet<string> = new Set(['claude', 'codex', 'all']);

// Read the persisted selection, or 'claude' when absent / unknown / malformed /
// storage-unavailable (private mode, quota, bad value).
export function loadActiveSource(): DashboardSelection {
  try {
    const raw = localStorage.getItem(SOURCE_STORAGE_KEY);
    if (raw != null && VALID_SELECTIONS.has(raw)) return raw as DashboardSelection;
  } catch {
    // localStorage unavailable / corrupt → fall back to the default.
  }
  return 'claude';
}

// Persist the selection (a bare literal). Swallows the rare localStorage
// exceptions (private-mode getItem/setItem throw, quota-exceeded setItem).
export function saveActiveSource(v: DashboardSelection): void {
  try {
    localStorage.setItem(SOURCE_STORAGE_KEY, v);
  } catch {
    // localStorage unavailable → the selection just won't survive a reload.
  }
}
