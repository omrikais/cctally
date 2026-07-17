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

// Board "wide" breakpoint — the width at/above which the tall row returns to
// the dense 3-across bento (Sessions span-6 / Trend span-3 / Projects span-3).
// Below it (900–1199, the "intermediate" band) Sessions is full-width and
// Trend/Projects pair. NORMATIVE (#293 S1): if the ui-qa gate shows a problem,
// revise the spec — do not retune this silently. This constant is the
// useBoardMode() hook's query, NOT a CSS twin: there is intentionally no 1200px
// @media rule; the CSS reuses the 900px 12-col grid and only the JS-driven
// data-span values differ between intermediate and bento.
export const BOARD_WIDE_PX = 1200;
export const BOARD_WIDE_MEDIA_QUERY = `(min-width: ${BOARD_WIDE_PX}px)`;

// #304 S1 — the conversation workspace's single-pane ↔ two-pane cutover. In two
// panes the rail resolves to its 340px max and shell overhead is ~44px (32px
// body padding + a 12px grid gap), so reader ≈ viewport − 384; a usable reader
// (≥480px) only exists above ~864px, so 880 gives a ~497px reader at the entry.
// Distinct from MOBILE (640, phone touch) and WIDE (1100, outline column /
// header density). NORMATIVE: revise via the spec if ui-qa shows a boundary
// problem — do not retune silently. There is no literal 880 CSS @media rule:
// the single-pane grid is class-driven (`.conv-view--mobile`), so nothing here
// needs a CSS twin.
export const COMPACT_WORKSPACE_PX = 880;
export const COMPACT_WORKSPACE_MEDIA_QUERY = `(max-width: ${COMPACT_WORKSPACE_PX}px)`;
