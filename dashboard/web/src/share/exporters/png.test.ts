// Spec §11.1 — client-side rasterization of an SVG body into a PNG blob.
//
// jsdom does not implement Image decoding or canvas.toBlob, so we stub
// the relevant Web Platform surfaces. The `stubProperty` helper captures
// the original descriptor and restores it on teardown so mutations don't
// leak into sibling test files (see the same pattern in ActionBar.test).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { svgToPng } from './png';

const propRefs: Array<[object, string, PropertyDescriptor | undefined]> = [];
function stubProperty<T extends object>(obj: T, key: string, value: unknown): void {
  propRefs.push([obj, key, Object.getOwnPropertyDescriptor(obj, key)]);
  Object.defineProperty(obj, key, { value, configurable: true });
}

afterEach(() => {
  vi.restoreAllMocks();
  while (propRefs.length > 0) {
    const [obj, key, desc] = propRefs.pop()!;
    if (desc) Object.defineProperty(obj, key, desc);
    else delete (obj as Record<string, unknown>)[key];
  }
});

describe('svgToPng', () => {
  beforeEach(() => {
    stubProperty(URL, 'createObjectURL', vi.fn().mockReturnValue('blob:fake'));
    stubProperty(URL, 'revokeObjectURL', vi.fn());
    // jsdom Image: decode() resolves immediately; naturalWidth/Height
    // fall through to whatever we set so canvas size assertions are
    // deterministic.
    stubProperty(Image.prototype as object, 'decode', vi.fn().mockResolvedValue(undefined));
    Object.defineProperty(Image.prototype, 'naturalWidth', { value: 100, configurable: true });
    Object.defineProperty(Image.prototype, 'naturalHeight', { value: 200, configurable: true });
    // canvas.toBlob in jsdom is undefined; stub directly.
    HTMLCanvasElement.prototype.toBlob = function (cb: BlobCallback) {
      cb(new Blob(['png-bytes'], { type: 'image/png' }));
    };
    HTMLCanvasElement.prototype.toDataURL = function () {
      return 'data:image/png;base64,fake';
    };
    HTMLCanvasElement.prototype.getContext = function () {
      const fns: Record<string, (...a: unknown[]) => void> = {
        fillRect: () => {}, scale: () => {}, drawImage: () => {},
      };
      return { ...fns, fillStyle: '#fff' } as unknown as CanvasRenderingContext2D;
    } as unknown as HTMLCanvasElement['getContext'];
  });

  it('returns a PNG blob via canvas.toBlob path', async () => {
    const out = await svgToPng('<svg />', 2, '#0f172a');
    expect(out.type).toBe('image/png');
  });

  it('canvas size = naturalWidth × scale, naturalHeight × scale', async () => {
    let observedW = 0;
    let observedH = 0;
    const real = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = real(tag);
      if (tag === 'canvas') {
        Object.defineProperty(el, 'width', {
          get() { return observedW; },
          set(v: number) { observedW = v; },
        });
        Object.defineProperty(el, 'height', {
          get() { return observedH; },
          set(v: number) { observedH = v; },
        });
      }
      return el;
    });
    await svgToPng('<svg />', 3, '#fff');
    expect(observedW).toBe(300);   // 100 × 3
    expect(observedH).toBe(600);   // 200 × 3
  });

  it('falls back to toDataURL → fetch when toBlob returns null', async () => {
    HTMLCanvasElement.prototype.toBlob = function (cb: BlobCallback) { cb(null); };
    // `new Response(blob)` carries the blob's MIME through Content-Type;
    // pass it explicitly so jsdom's Response.blob() preserves
    // 'image/png' on the round-trip (otherwise it falls back to
    // text/plain;charset=utf-8 from the default Content-Type).
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new Blob(['png-fallback'], { type: 'image/png' }), {
        headers: { 'Content-Type': 'image/png' },
      }),
    );
    const out = await svgToPng('<svg />', 2, '#fff');
    expect(fetchSpy).toHaveBeenCalledWith('data:image/png;base64,fake');
    expect(out.type).toBe('image/png');
  });

  it('revokes the SVG blob URL on success', async () => {
    const revoke = URL.revokeObjectURL as ReturnType<typeof vi.fn>;
    await svgToPng('<svg />', 2, '#fff');
    expect(revoke).toHaveBeenCalledWith('blob:fake');
  });

  it('revokes the SVG blob URL on error', async () => {
    HTMLCanvasElement.prototype.getContext = (function () {
      return null;
    }) as unknown as HTMLCanvasElement['getContext'];
    const revoke = URL.revokeObjectURL as ReturnType<typeof vi.fn>;
    await expect(svgToPng('<svg />', 2, '#fff')).rejects.toThrow();
    expect(revoke).toHaveBeenCalledWith('blob:fake');
  });
});
