// #463 S4 F-A — the element a landmark jump should align, INSIDE the item the
// jump loaded.
//
// The gate measured the shipped behaviour: a click on an error rail row put the
// correct segment item at the top of a 635px viewport and left the failure it
// named 1,984px, 5,516px or 6,574px below the fold, because one Codex segment
// can be 4,098px tall and hold several failing calls. Landing on the item is
// therefore the right region and the wrong position, and the block key each
// landmark already carries is the finer address.
//
// Two attributes, because the reader already mints two identities at this
// granularity: `data-block-key` on a physical row's rendered block, and
// `data-heading-key` on one decomposed reasoning heading, whose key is
// `<block_key>#<ordinal>` — the same string the outline publishes as a
// reasoning landmark's `landmark_key`.

// A tool card is rendered by one of a dozen components, so the attribute rides
// a wrapper rather than each card root. The wrapper is `display: contents`, so
// it generates no box and cannot change the layout — which also means it has no
// rect of its own to align, and the caller must descend to the child it names.
export const BLOCK_ANCHOR_CLASS = 'conv-block-anchor';

// How deep a chain of box-less wrappers this will walk before giving up. The
// markup has one today; the bound exists so a cycle-free but pathological tree
// cannot turn a jump into an unbounded walk.
const MAX_BOX_LESS_DEPTH = 4;

// Whether this element generates a box of its own. That, not a class name, is
// what decides whether an element can be aligned: an element with no box has no
// rect, and `scrollIntoView` on it reports the origin, which is how the browser
// gate measured jumps landing 1,984-6,574px away from what they named.
//
// `display: contents` is the value that produces exactly this state — the
// element itself generates no box while its children still do — so it is the
// property tested. The class is kept as a second signal rather than as THE
// signal, because JSDOM applies no stylesheet: under test the wrapper's computed
// display is its default, and only the class says what the real cascade will do.
function generatesNoBox(el: HTMLElement): boolean {
  if (el.classList.contains(BLOCK_ANCHOR_CLASS)) return true;
  const view = el.ownerDocument?.defaultView;
  if (view == null) return false;
  return view.getComputedStyle(el).display === 'contents';
}

export function resolveJumpAnchor(
  item: Element | null | undefined,
  anchorKey: string | null | undefined,
): HTMLElement | null {
  if (item == null || !anchorKey) return null;
  const key = CSS.escape(anchorKey);
  const found = item.querySelector(`[data-block-key="${key}"], [data-heading-key="${key}"]`);
  if (!(found instanceof HTMLElement)) return null;
  let el: HTMLElement = found;
  for (let depth = 0; depth < MAX_BOX_LESS_DEPTH && generatesNoBox(el); depth++) {
    // A box-less element with no element child names no alignable region at
    // all, so the caller falls back to the item — which is the pre-S4 landing.
    const child = el.firstElementChild;
    if (!(child instanceof HTMLElement)) return null;
    el = child;
  }
  return el;
}
