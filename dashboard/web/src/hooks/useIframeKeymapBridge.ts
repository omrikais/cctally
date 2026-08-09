import { useEffect } from 'react';
import { dispatchKeydown } from '../store/keymap';

/**
 * Forward `Escape` out of a same-origin preview iframe into the central keymap.
 *
 * WHY THIS EXISTS: every Esc binding resolves through ONE listener on the
 * PARENT document (`installGlobalKeydown`). A keydown raised inside a `srcdoc`
 * iframe belongs to a different document and never reaches it, so Esc stopped
 * working the moment the user clicked into the preview (#503 S4 / F28).
 *
 * PRECONDITION: the iframe carries `sandbox="allow-same-origin"`. Without that
 * token `contentDocument` is inaccessible and this bridge silently does
 * nothing. That coupling is asserted at each call site's tests and recorded in
 * docs/share-gotchas.md — do not "harden" the sandbox without reading it.
 *
 * ESCAPE ONLY, deliberately. Two reasons: forwarding every key would turn a
 * click into the preview followed by `s` into an unrelated Settings toggle;
 * and `isTextInputFocused` tests `target instanceof Element` against the PARENT
 * realm, which a child-document target always fails, so a forwarded printable
 * key would bypass input-mode suppression entirely.
 */
export function useIframeKeymapBridge(el: HTMLIFrameElement | null): void {
  useEffect(() => {
    if (!el) return;

    let attachedDoc: Document | null = null;

    const onChildKeydown = (e: Event): void => {
      const ke = e as KeyboardEvent;
      if (ke.key !== 'Escape') return;
      dispatchKeydown(ke);
    };

    const detach = (): void => {
      if (!attachedDoc) return;
      attachedDoc.removeEventListener('keydown', onChildKeydown);
      attachedDoc = null;
    };

    // Always detach first: `srcdoc` changes on every successful render, and
    // each change is a navigation producing a NEW document. Leaving the old
    // listener attached leaks one per render.
    const attach = (): void => {
      detach();
      let doc: Document | null = null;
      try {
        doc = el.contentDocument;
      } catch {
        doc = null;              // cross-origin: bridge unavailable, not fatal
      }
      if (!doc) return;
      doc.addEventListener('keydown', onChildKeydown);
      attachedDoc = doc;
    };

    el.addEventListener('load', attach);

    // The first load can complete before this effect runs, in which case no
    // further `load` fires until the next render. Attach immediately when the
    // document has one to attach to; `attach` detaches first, so a subsequent
    // `load` cannot double-attach.
    //
    // The test is `!== 'loading'`, not `=== 'complete'`. A document caught at
    // 'interactive' — parsed, still fetching subresources — has a usable
    // `document` and has already fired the events this effect would otherwise
    // wait for, so gating on 'complete' alone drops the bridge until the next
    // `srcDoc` change. Attaching at 'loading' is the only case worth skipping,
    // because that document is about to be replaced by the srcdoc navigation
    // whose `load` attaches properly.
    try {
      if (el.contentDocument && el.contentDocument.readyState !== 'loading') attach();
    } catch {
      /* cross-origin — nothing to attach to */
    }

    return () => {
      el.removeEventListener('load', attach);
      detach();
    };
  }, [el]);
}
