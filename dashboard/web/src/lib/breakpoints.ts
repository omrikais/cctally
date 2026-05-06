// Single source of truth for the mobile breakpoint. CSS uses the literal
// `640px` in @media rules at the bottom of index.css; this module is the
// JS twin so tests and the useIsMobile hook stay in sync if the value
// ever moves. Update both sides together.
export const MOBILE_BREAKPOINT_PX = 640;
export const MOBILE_MEDIA_QUERY = `(max-width: ${MOBILE_BREAKPOINT_PX}px)`;
