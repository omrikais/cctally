// ActionBar — plan §M1.15 contract:
//   - Copy fires navigator.clipboard.writeText (MD only).
//   - Download builds a Blob and anchor-clicks (file ext matches format).
//   - Open spawns window.open with a blob URL (HTML/SVG only).
//   - Disabled-in-M1 buttons (PNG, Print → PDF) have explanatory
//     tooltips and disabled attribute. (Save preset went live in M2.4;
//     + Basket went live in M3.5.)
//   - Format radio dispatches onOptionsChange with new format.
import { render, screen, fireEvent, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActionBar } from './ActionBar';
import { _resetForTests } from '../store/store';
import type { ShareOptions } from './types';

// Mirrors production `defaultShareOptions()` in ShareModal.tsx — spec
// Q7 / §6.3: anon-by-default on export, so `reveal_projects: false`.
function defaults(): ShareOptions {
  return {
    format: 'md',
    theme: 'light',
    reveal_projects: false,
    no_branding: false,
    top_n: 5,
    period: { kind: 'current' },
    project_allowlist: null,
    show_chart: true,
    show_table: true,
  };
}

// Capture original property descriptors so we can restore them between
// tests. `vi.restoreAllMocks()` only undoes spies/stubs, not direct
// `Object.defineProperty` mutations — without these, navigator.clipboard
// and URL.createObjectURL leak into sibling test files in the same
// worker. (Discovered during the M1.15 code review pass.)
const propRefs: Array<[object, string, PropertyDescriptor | undefined]> = [];
function stubProperty<T extends object>(obj: T, key: string, value: unknown): void {
  propRefs.push([obj, key, Object.getOwnPropertyDescriptor(obj, key)]);
  Object.defineProperty(obj, key, { value, configurable: true });
}

beforeEach(() => {
  _resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  while (propRefs.length > 0) {
    const [obj, key, desc] = propRefs.pop()!;
    if (desc) Object.defineProperty(obj, key, desc);
    else delete (obj as Record<string, unknown>)[key];
  }
});

describe('<ActionBar>', () => {
  it('Copy button writes the rendered body to the clipboard', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        body: '# Weekly\n\nbody',
        content_type: 'text/markdown',
        snapshot: {},
      }),
    }));
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubProperty(navigator, 'clipboard', { writeText });

    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^copy$/i }));
    });
    expect(writeText).toHaveBeenCalledWith('# Weekly\n\nbody');
  });

  it('Download builds a Blob and clicks a hidden anchor', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        body: '# md body',
        content_type: 'text/markdown',
        snapshot: {},
      }),
    }));
    // Stub URL.createObjectURL / revokeObjectURL — jsdom doesn't.
    const createObjectURL = vi.fn().mockReturnValue('blob:fake-url');
    const revokeObjectURL = vi.fn();
    stubProperty(URL, 'createObjectURL', createObjectURL);
    stubProperty(URL, 'revokeObjectURL', revokeObjectURL);
    // Spy on the anchor click — jsdom DOES support .click().
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^download$/i }));
    });
    expect(createObjectURL).toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalled();
  });

  it('Open spawns window.open for HTML format', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        body: '<html>x</html>',
        content_type: 'text/html',
        snapshot: {},
      }),
    }));
    const createObjectURL = vi.fn().mockReturnValue('blob:fake-url');
    stubProperty(URL, 'createObjectURL', createObjectURL);
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);

    const opts: ShareOptions = { ...defaults(), format: 'html' };
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={opts}
        onOptionsChange={() => {}}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^open$/i }));
    });
    expect(openSpy).toHaveBeenCalledWith(
      'blob:fake-url',
      '_blank',
      'noopener,noreferrer',
    );
  });

  it('Format-gated action buttons carry explanatory tooltips', () => {
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    // M4.1 — PNG is live but format-gated to SVG. Default options use
    // format='md', so the button is disabled with a "SVG only" tooltip.
    const png = screen.getByRole('button', { name: /^png$/i });
    expect(png).toBeDisabled();
    expect(png.getAttribute('title')).toMatch(/svg format only/i);

    // M4.2 — Print → PDF is live but format-gated to HTML. With default
    // format='md', the button is disabled with an "HTML only" tooltip.
    const print = screen.getByRole('button', { name: /print/i });
    expect(print).toBeDisabled();
    expect(print.getAttribute('title')).toMatch(/html format only/i);

    // M3.5 — + Basket is live (no longer disabled). With a
    // templateId in scope it should be enabled and carry the
    // descriptive tooltip rather than the legacy "coming in M3" stub.
    const basket = screen.getByRole('button', { name: /\+ basket/i });
    expect(basket).not.toBeDisabled();
    expect(basket.getAttribute('title')).toMatch(/add this section to the report basket/i);

    // M2.4 — Save preset is live (no longer disabled). With a
    // templateId in scope it should be enabled and carry the
    // descriptive tooltip rather than the legacy "coming in M2" stub.
    const preset = screen.getByRole('button', { name: /save preset/i });
    expect(preset).not.toBeDisabled();
    expect(preset.getAttribute('title')).toMatch(/save the current recipe/i);
  });

  it('Save preset is disabled when no template is selected', () => {
    render(
      <ActionBar
        panel="weekly"
        templateId={null}
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    const preset = screen.getByRole('button', { name: /save preset/i });
    expect(preset).toBeDisabled();
    expect(preset.getAttribute('title')).toMatch(/template/i);
  });

  it('Save preset click opens the inline popover', () => {
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    const preset = screen.getByRole('button', { name: /save preset/i });
    fireEvent.click(preset);
    // Popover renders a role=dialog with aria-label "Save preset".
    expect(screen.getByRole('dialog', { name: /save preset/i })).toBeInTheDocument();
  });

  it('Format radio dispatches onOptionsChange with new format', () => {
    const onOptionsChange = vi.fn();
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={onOptionsChange}
      />,
    );
    // Radios labeled by their format slug.
    const htmlRadio = screen.getByLabelText('html');
    fireEvent.click(htmlRadio);
    expect(onOptionsChange).toHaveBeenCalledWith(
      expect.objectContaining({ format: 'html' }),
    );
  });

  it('Copy is disabled for non-MD formats', () => {
    const opts: ShareOptions = { ...defaults(), format: 'html' };
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={opts}
        onOptionsChange={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /^copy$/i })).toBeDisabled();
  });

  it('Open is disabled for MD format', () => {
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /^open$/i })).toBeDisabled();
  });

  it('PNG button is enabled for SVG and rasterizes via canvas', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        body: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>',
        content_type: 'image/svg+xml',
        snapshot: {},
      }),
    }));
    // Stub the URL + Image + canvas surfaces svgToPng touches.
    const createObjectURL = vi.fn().mockReturnValue('blob:fake');
    const revokeObjectURL = vi.fn();
    stubProperty(URL, 'createObjectURL', createObjectURL);
    stubProperty(URL, 'revokeObjectURL', revokeObjectURL);
    stubProperty(Image.prototype as object, 'decode', vi.fn().mockResolvedValue(undefined));
    Object.defineProperty(Image.prototype, 'naturalWidth', { value: 50, configurable: true });
    Object.defineProperty(Image.prototype, 'naturalHeight', { value: 50, configurable: true });
    HTMLCanvasElement.prototype.toBlob = function (cb: BlobCallback) {
      cb(new Blob(['png'], { type: 'image/png' }));
    };
    HTMLCanvasElement.prototype.getContext = function () {
      return {
        fillRect: () => {}, scale: () => {}, drawImage: () => {}, fillStyle: '#fff',
      } as unknown as CanvasRenderingContext2D;
    } as unknown as HTMLCanvasElement['getContext'];
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    const opts: ShareOptions = { ...defaults(), format: 'svg' };
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={opts}
        onOptionsChange={() => {}}
      />,
    );
    const png = screen.getByRole('button', { name: /^png$/i });
    expect(png).not.toBeDisabled();
    await act(async () => {
      fireEvent.click(png);
    });
    expect(clickSpy).toHaveBeenCalled();
  });

  it('Print → PDF button is enabled for HTML and opens the print dialog', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        body: '<!DOCTYPE html><html><body>x</body></html>',
        content_type: 'text/html',
        snapshot: {},
      }),
    }));
    // Patch the appendChild that printPdf uses so we can stub
    // contentWindow.print before it's invoked synchronously.
    const printSpy = vi.fn();
    const origAppend = HTMLBodyElement.prototype.appendChild;
    vi.spyOn(HTMLBodyElement.prototype, 'appendChild').mockImplementation(function (this: HTMLBodyElement, node: Node) {
      const ret = origAppend.call(this, node) as Node;
      if (node instanceof HTMLIFrameElement && node.contentWindow) {
        Object.defineProperty(node.contentWindow, 'print', { value: printSpy, configurable: true });
        Object.defineProperty(node.contentWindow, 'focus', { value: () => {}, configurable: true });
      }
      return ret;
    });

    const opts: ShareOptions = { ...defaults(), format: 'html' };
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={opts}
        onOptionsChange={() => {}}
      />,
    );
    const print = screen.getByRole('button', { name: /print/i });
    expect(print).not.toBeDisabled();
    await act(async () => {
      fireEvent.click(print);
    });
    expect(printSpy).toHaveBeenCalledTimes(1);
    // Cleanup: drain the iframe the handler appended.
    document.querySelectorAll('iframe').forEach((f) => f.remove());
  });
});
