// #463 S2 §2.7 — "the step landed" must mean the heading is VISIBLE, not merely
// present in the document.
//
// `querySelectorAll` resolves an element inside a CLOSED `<details>`, because
// HTML keeps a shut disclosure's children in the DOM. A heading step onto one
// therefore reported success, advanced the cursor and produced no mark and no
// scroll, which made the navigation invariant "in the DOM" rather than
// "visible". SidechainGroup.tsx records the same hazard for the jump pipeline.
//
// `Element.checkVisibility` is the browser's own answer and wins wherever it
// exists. jsdom 25 implements neither it nor layout — it reports `offsetParent`
// as null for every element, so the usual offsetParent test cannot stand in — so
// the fallback below walks the ancestor chain for a shut `<details>`, which is
// the one hidden-container shape the reader's tree builds. Content inside that
// disclosure's `<summary>` stays on screen (the expandable reasoning block puts
// its headings there), so the walk only rejects a node reached through a
// non-summary child.
export function headingIsVisible(el: Element): boolean {
  const probe = el as Element & { checkVisibility?: () => boolean };
  if (typeof probe.checkVisibility === 'function') return probe.checkVisibility();
  let child: Element = el;
  for (let node = el.parentElement; node != null; child = node, node = node.parentElement) {
    if (node.tagName === 'DETAILS'
        && !(node as HTMLDetailsElement).open
        && child.tagName !== 'SUMMARY') {
      return false;
    }
  }
  return true;
}
