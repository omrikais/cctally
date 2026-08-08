// Spec §11.2 — browser-native print via hidden iframe.
//
// The kernel's `_print_stylesheet()` is already in the document <head>
// (wired in M4.2), so we just hand the HTML to the iframe and call
// print(). The iframe is removed after a 1s timeout so the user has
// time to interact with the print dialog. (Removing earlier would close
// the dialog.)
//
// Fallback when the iframe cannot print (some embedded browsers, certain
// Safari configurations): open the body in a new window. The new window
// inherits the kernel's @media print rules so the result is equivalent —
// the user hits Cmd/Ctrl+P themselves.
//
// #503 S3 §5 — THIS FUNCTION NOW REPORTS FAILURE. It used to optional-chain
// a missing `contentWindow` and return successfully, and its fallback
// discarded a blocked `window.open`. `ActionBar` recorded share history
// immediately after this void helper returned, so a print that never
// happened was recorded as one, and adding a success toast to that would
// only have made a false success louder. Every path that does not reach a
// print dialog or a fallback window throws.
//
// The fallback opens its window through `openDetachedTab`, the one helper in
// this directory that opens a tab in a way whose result can be TESTED. Passing
// `noopener` — which this fallback used to do — makes `window.open` return
// null by specification, so the null check reported every successful fallback
// as a blocked popup: a real browser opened the tab, this function threw
// before writing the body, and the user was left with a blank tab and a
// "Print failed" banner.
import { POPUP_BLOCKED_MESSAGE, openDetachedTab } from './openTab';

export function printPdf(htmlBody: string): void {
  const iframe = document.createElement('iframe');
  iframe.setAttribute('aria-hidden', 'true');
  iframe.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0';
  document.body.appendChild(iframe);
  const doc = iframe.contentDocument;
  if (!doc) {
    iframe.remove();
    throw new Error('iframe contentDocument unavailable');
  }
  doc.open();
  doc.write(htmlBody);
  doc.close();

  const win = iframe.contentWindow;
  let printed = false;
  if (win && typeof win.print === 'function') {
    try {
      if (typeof win.focus === 'function') win.focus();
      win.print();
      printed = true;
    } catch {
      printed = false;
    }
  }

  if (!printed) {
    // No print target at all, or the iframe refused. Either way the user has
    // seen nothing yet, so the fallback is the last chance to succeed. The
    // iframe printed nothing and nothing else will use it, so it goes now on
    // both the blocked and the successful branch rather than in a second.
    iframe.remove();
    const w = openDetachedTab();
    if (!w) throw new Error(POPUP_BLOCKED_MESSAGE);
    w.document.open();
    w.document.write(htmlBody);
    w.document.close();
    return;
  }

  setTimeout(() => iframe.remove(), 1000);
}
