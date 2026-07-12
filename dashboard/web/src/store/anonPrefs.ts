// #281 S4 — the conversation "Anonymize" mode preference. A single
// localStorage-backed boolean in the `cctally.conv.*` namespace, mirroring
// findPrefs.ts. Default is ON (safe sharing by default): an ABSENT key reads as
// true, and any storage exception falls back to the in-memory default (true) so
// a blocked localStorage never silently turns anonymization OFF.

export const ANON_MODE_KEY = 'cctally.conv.anonMode';

export function loadAnonMode(): boolean {
  try {
    const v = localStorage.getItem(ANON_MODE_KEY);
    return v === null ? true : v === '1'; // default ON when unset
  } catch {
    return true; // storage unavailable / blocked → default ON
  }
}

export function saveAnonMode(value: boolean): void {
  try {
    localStorage.setItem(ANON_MODE_KEY, value ? '1' : '0');
  } catch {
    // storage unavailable → the pref just won't survive a reload (stays ON).
  }
}
