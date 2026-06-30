// dashboard/web/src/test-utils/intersectionObserver.ts
// jsdom lacks IntersectionObserver — a minimal stub so observer-driven effects
// (the reader's lazy-load sentinel, #248's HeroStrip sticky-collapse) can mount
// without throwing. Call installIntersectionObserverStub() in beforeEach
// (afterEach's vi.restoreAllMocks/unstubAllGlobals is not required to undo it,
// but a fresh install per test keeps it deterministic).
//
// #248 — the stub is also CONTROLLABLE: it records every live instance and the
// element each observes, and `emit(isIntersecting)` synchronously fires the
// stored callback, so a test can drive the HeroStrip observer's
// SET_HERO_SCROLLED dispatch without a real browser. `installIntersection
// ObserverStub()` clears the instance registry so each test starts clean.
export class IntersectionObserverStub {
  static instances: IntersectionObserverStub[] = [];
  private cb: IntersectionObserverCallback;
  readonly observed: Element[] = [];
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb;
    IntersectionObserverStub.instances.push(this);
  }
  observe(el: Element): void { this.observed.push(el); }
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] { return []; }
  // Test driver — fire the callback for every observed element with the given
  // intersection state.
  emit(isIntersecting: boolean): void {
    const entries = this.observed.map(
      (target) => ({ isIntersecting, target } as unknown as IntersectionObserverEntry),
    );
    this.cb(entries, this as unknown as IntersectionObserver);
  }
}

export function installIntersectionObserverStub(): void {
  IntersectionObserverStub.instances = [];
  (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserverStub })
    .IntersectionObserver = IntersectionObserverStub;
}
