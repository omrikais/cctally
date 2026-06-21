// #217 S3 E6(b) — the resizable outline column. A tiny localStorage-backed
// persistence module for the outline panel's WIDTH (the 3rd grid track of
// `.conv-view--outline`), mirroring readingPosition.ts. The width is clamped to
// [MIN, MAX]; the store seeds `convOutlineWidth` from `loadOutlineWidth()` and
// persists via `saveOutlineWidth()` on every SET_CONV_OUTLINE_WIDTH.

// New surface → the `cctally.*` namespace (NOT the legacy `ccusage.*` blob).
export const OUTLINE_WIDTH_KEY = 'cctally.conv.outlineWidth';

// Anchored on today's `.conv-view--outline` 3rd track `minmax(230px, 290px)`:
// MIN is that minmax floor; DEFAULT is its ceiling (so an un-resized panel reads
// identically to before); MAX gives generous headroom for a wide outline. The
// drag/keyboard step is a comfortable 16px.
export const OUTLINE_WIDTH_MIN = 230;
export const OUTLINE_WIDTH_DEFAULT = 290;
export const OUTLINE_WIDTH_MAX = 480;
export const OUTLINE_WIDTH_STEP = 16;

// Clamp + integer-round any candidate width into the legal band.
export function clampOutlineWidth(px: number): number {
  if (!Number.isFinite(px)) return OUTLINE_WIDTH_DEFAULT;
  return Math.round(Math.min(OUTLINE_WIDTH_MAX, Math.max(OUTLINE_WIDTH_MIN, px)));
}

// Read the persisted width (clamped), or the default when absent / corrupt /
// unavailable (private mode, quota, bad value).
export function loadOutlineWidth(): number {
  try {
    const raw = localStorage.getItem(OUTLINE_WIDTH_KEY);
    if (raw == null) return OUTLINE_WIDTH_DEFAULT;
    const n = Number(raw);
    if (Number.isFinite(n)) return clampOutlineWidth(n);
  } catch {
    // localStorage unavailable / corrupt → fall back to the default.
  }
  return OUTLINE_WIDTH_DEFAULT;
}

// Persist the width (already clamped by the caller / clampOutlineWidth).
export function saveOutlineWidth(px: number): void {
  try {
    localStorage.setItem(OUTLINE_WIDTH_KEY, String(clampOutlineWidth(px)));
  } catch {
    // localStorage unavailable → the width just won't survive a reload.
  }
}
