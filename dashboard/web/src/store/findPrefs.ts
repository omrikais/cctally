// #217 S4 / I-1.4 — the in-conversation find bar's regex / case toggle prefs.
// Two tiny localStorage-backed boolean scalars, mirroring outlineWidth.ts, so a
// power user's regex/case-sensitive preference survives a reload. The find bar
// seeds its toggle state from these on mount and persists on every flip.

// New surface → the `cctally.*` namespace (NOT the legacy `ccusage.*` blob).
export const FIND_REGEX_KEY = 'cctally.conv.find.regex';
export const FIND_CASE_KEY = 'cctally.conv.find.case';

function loadBool(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    // localStorage unavailable / blocked → default off.
    return false;
  }
}

function saveBool(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? '1' : '0');
  } catch {
    // localStorage unavailable → the pref just won't survive a reload.
  }
}

export function loadFindRegex(): boolean { return loadBool(FIND_REGEX_KEY); }
export function saveFindRegex(b: boolean): void { saveBool(FIND_REGEX_KEY, b); }
export function loadFindCase(): boolean { return loadBool(FIND_CASE_KEY); }
export function saveFindCase(b: boolean): void { saveBool(FIND_CASE_KEY, b); }
