// Module-level flag set immediately after a drag finishes, so the click
// synthesized at the end of the gesture is swallowed by the dragged or
// destination panel's onClickCapture handler instead of opening its modal.
// Cleared on the next macrotask (setTimeout 0) so legitimate clicks fired
// after a tick land normally.

let _suppressNextClick = false;

export function shouldSuppressNextClick(): boolean {
  return _suppressNextClick;
}

export function armClickSuppression(): void {
  _suppressNextClick = true;
  setTimeout(() => { _suppressNextClick = false; }, 0);
}

export function _resetClickSuppressionForTests(): void {
  _suppressNextClick = false;
}
