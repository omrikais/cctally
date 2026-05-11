// Spec §11.2 — printPdf mounts the HTML body in a hidden iframe and
// calls the browser's native print() dialog. We can't drive a real
// print dialog in jsdom, but we can assert the iframe lifecycle:
// appended to <body>, contentDocument.write called, print() called,
// removed after the 1s timeout.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { printPdf } from './printPdf';

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  // Drain any iframes the test left behind.
  document.querySelectorAll('iframe').forEach((f) => f.remove());
});

describe('printPdf', () => {
  it('mounts an iframe and calls print on the contentWindow', () => {
    const printSpy = vi.fn();
    // jsdom returns null contentWindow until the iframe is in the DOM —
    // intercept appendChild so we can stub print/focus on the newly
    // mounted iframe's window before printPdf's call lands.
    const origAppend = HTMLBodyElement.prototype.appendChild;
    const appendSpy = vi.spyOn(HTMLBodyElement.prototype, 'appendChild').mockImplementation(function (this: HTMLBodyElement, node: Node) {
      const ret = origAppend.call(this, node) as Node;
      if (node instanceof HTMLIFrameElement && node.contentWindow) {
        Object.defineProperty(node.contentWindow, 'print', {
          value: printSpy,
          configurable: true,
        });
        Object.defineProperty(node.contentWindow, 'focus', {
          value: () => {},
          configurable: true,
        });
      }
      return ret;
    });

    printPdf('<html><body><h1>x</h1></body></html>');

    expect(appendSpy).toHaveBeenCalled();
    expect(printSpy).toHaveBeenCalledTimes(1);
  });

  it('writes the supplied HTML into the iframe document', () => {
    // Stub print/focus on every new iframe so jsdom's not-implemented
    // warnings stay out of test output.
    const origAppend = HTMLBodyElement.prototype.appendChild;
    vi.spyOn(HTMLBodyElement.prototype, 'appendChild').mockImplementation(function (this: HTMLBodyElement, node: Node) {
      const ret = origAppend.call(this, node) as Node;
      if (node instanceof HTMLIFrameElement && node.contentWindow) {
        Object.defineProperty(node.contentWindow, 'print', { value: () => {}, configurable: true });
        Object.defineProperty(node.contentWindow, 'focus', { value: () => {}, configurable: true });
      }
      return ret;
    });
    printPdf('<html><body><h1>printable</h1></body></html>');
    const iframe = document.querySelector('iframe');
    expect(iframe).not.toBeNull();
    // jsdom's iframe.contentDocument inherits from `about:blank`; after
    // doc.write the body should contain our supplied content.
    const body = iframe!.contentDocument?.body;
    expect(body?.innerHTML).toContain('<h1>printable</h1>');
  });

  it('removes the iframe after 1s', () => {
    const origAppend = HTMLBodyElement.prototype.appendChild;
    vi.spyOn(HTMLBodyElement.prototype, 'appendChild').mockImplementation(function (this: HTMLBodyElement, node: Node) {
      const ret = origAppend.call(this, node) as Node;
      if (node instanceof HTMLIFrameElement && node.contentWindow) {
        Object.defineProperty(node.contentWindow, 'print', { value: () => {}, configurable: true });
        Object.defineProperty(node.contentWindow, 'focus', { value: () => {}, configurable: true });
      }
      return ret;
    });
    printPdf('<html></html>');
    const before = document.querySelectorAll('iframe').length;
    expect(before).toBe(1);
    vi.advanceTimersByTime(1000);
    const after = document.querySelectorAll('iframe').length;
    expect(after).toBe(0);
  });

  it('falls back to window.open when iframe.print throws', () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    const origAppend = HTMLBodyElement.prototype.appendChild;
    vi.spyOn(HTMLBodyElement.prototype, 'appendChild').mockImplementation(function (this: HTMLBodyElement, node: Node) {
      const ret = origAppend.call(this, node) as Node;
      if (node instanceof HTMLIFrameElement && node.contentWindow) {
        Object.defineProperty(node.contentWindow, 'print', {
          value: () => { throw new Error('print blocked'); },
          configurable: true,
        });
        Object.defineProperty(node.contentWindow, 'focus', {
          value: () => {},
          configurable: true,
        });
      }
      return ret;
    });

    printPdf('<html><body>fallback</body></html>');

    expect(openSpy).toHaveBeenCalledWith('', '_blank', 'noopener,noreferrer');
  });
});
