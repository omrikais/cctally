import '@testing-library/jest-dom/vitest';

// jsdom polyfill for ResizeObserver (used by RangeBar in Task 9).
// Must run before any component is mounted in a test.
class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
(globalThis as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

// jsdom does not implement Element.prototype.scrollIntoView. The sessions
// panel calls it when the n/N search navigation advances the index; without
// this stub, any test that touches that code path throws
// "scrollIntoView is not a function". Noop for tests — visual scroll
// behavior isn't asserted here.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function noop(): void {};
}

// jsdom's requestAnimationFrame does not actually schedule its callback in this
// vitest config (callbacks never run), which would hang any code that awaits a
// rAF loop — e.g. the reader's #234 layout-quiesce waiter. Install a setTimeout-
// backed shim so rAF-driven code resolves under test (visual timing isn't
// asserted here; pixel landing is the Playwright gate's job).
{
  const g = globalThis as unknown as {
    requestAnimationFrame?: (cb: FrameRequestCallback) => number;
    cancelAnimationFrame?: (h: number) => void;
  };
  let pending = false;
  const probe = g.requestAnimationFrame;
  if (typeof probe === 'function') {
    // Detect a no-op rAF (jsdom): if a scheduled callback hasn't been observed
    // synchronously we can't know here, so just always install the shim — a
    // setTimeout-backed rAF is correct for tests regardless.
    pending = true;
  }
  if (pending || typeof probe !== 'function') {
    g.requestAnimationFrame = (cb: FrameRequestCallback): number =>
      setTimeout(() => cb(performance.now()), 0) as unknown as number;
    g.cancelAnimationFrame = (h: number): void => clearTimeout(h as unknown as NodeJS.Timeout);
  }
}

// Node 25 ships an experimental built-in `localStorage` global that takes
// precedence over jsdom's Storage implementation — the built-in stub lacks
// `getItem`/`setItem` unless `--localstorage-file` is passed at boot.
// Install an in-memory Storage shim so store tests (and any localStorage-using
// module code) work regardless of Node version. Defining on globalThis
// instead of window ensures it wins even when Node's stub claims the name.
class MemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length(): number { return this.map.size; }
  clear(): void { this.map.clear(); }
  getItem(key: string): string | null {
    return this.map.has(key) ? this.map.get(key)! : null;
  }
  key(i: number): string | null {
    return Array.from(this.map.keys())[i] ?? null;
  }
  removeItem(key: string): void { this.map.delete(key); }
  setItem(key: string, value: string): void { this.map.set(key, String(value)); }
}
Object.defineProperty(globalThis, 'localStorage', {
  value: new MemoryStorage(),
  writable: true,
  configurable: true,
});
Object.defineProperty(globalThis, 'sessionStorage', {
  value: new MemoryStorage(),
  writable: true,
  configurable: true,
});
