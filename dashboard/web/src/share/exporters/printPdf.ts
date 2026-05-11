// Spec §11.2 — browser-native print via hidden iframe.
//
// The kernel's `_print_stylesheet()` is already in the document <head>
// (wired in M4.2), so we just hand the HTML to the iframe and call
// print(). The iframe is removed after a 1s timeout so the user has
// time to interact with the print dialog. (Removing earlier would close
// the dialog.)
//
// Fallback when iframe.contentWindow.print throws (some embedded
// browsers / certain Safari configurations): open the body in a new
// window. The new window inherits the kernel's @media print rules so
// the result is equivalent — the user hits Cmd/Ctrl+P themselves.

export function printPdf(htmlBody: string): void {
  const iframe = document.createElement('iframe');
  iframe.setAttribute('aria-hidden', 'true');
  iframe.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0';
  document.body.appendChild(iframe);
  const doc = iframe.contentDocument;
  if (!doc) throw new Error('iframe contentDocument unavailable');
  doc.open();
  doc.write(htmlBody);
  doc.close();
  try {
    iframe.contentWindow?.focus();
    iframe.contentWindow?.print();
  } catch {
    // Fallback: open in new window. The new window's print() is the
    // standard browser flow; user can hit Cmd/Ctrl+P themselves.
    const w = window.open('', '_blank', 'noopener,noreferrer');
    if (w) {
      w.document.open();
      w.document.write(htmlBody);
      w.document.close();
    }
  }
  setTimeout(() => iframe.remove(), 1000);
}
