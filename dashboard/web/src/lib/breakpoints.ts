// Single source of truth for the mobile breakpoint. CSS uses the literal
// `640px` in @media rules at the bottom of index.css; this module is the
// JS twin so tests and the useIsMobile hook stay in sync if the value
// ever moves. Update both sides together.
export const MOBILE_BREAKPOINT_PX = 640;
export const MOBILE_MEDIA_QUERY = `(max-width: ${MOBILE_BREAKPOINT_PX}px)`;

// #217 S7 F10 — the comparison view's two-column ↔ unified switch. Distinct from
// the 640px mobile cutover: the side-by-side prompt diff needs real horizontal
// room, so below ~1100px it falls back to the unified single column. The
// `useIsWide` hook reads WIDE_MEDIA_QUERY (true === two-column); the CSS twin is
// the `@media (max-width: 1100px)` safety block in index.css. `+1` so the JS
// `min-width` boundary and the CSS `max-width: 1100px` cutoff don't overlap on
// the 1100px pixel itself (1101+ === wide; ≤1100 === unified).
export const WIDE_BREAKPOINT_PX = 1100;
export const WIDE_MEDIA_QUERY = `(min-width: ${WIDE_BREAKPOINT_PX + 1}px)`;

// Desktop bento breakpoint — the JS twin of the CSS `@media (min-width: 900px)`
// that promotes the board to the height-matched 12-col bento (index.css:396).
// Used by the `c`-keymap gate (collapse is a no-op in the bento) and the Daily
// card's compact-cost mode. Update both sides together if it ever moves.
export const BENTO_BREAKPOINT_PX = 900;
export const BENTO_MEDIA_QUERY = `(min-width: ${BENTO_BREAKPOINT_PX}px)`;
