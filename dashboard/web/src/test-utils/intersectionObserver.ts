// dashboard/web/src/test-utils/intersectionObserver.ts
// jsdom lacks IntersectionObserver — a minimal no-op so the reader's lazy-load
// sentinel effect can mount without throwing. Call installIntersectionObserverStub()
// in beforeEach (afterEach's vi.restoreAllMocks/unstubAllGlobals is not required
// to undo it, but a fresh install per test keeps it deterministic).
export class IntersectionObserverStub {
  constructor(_cb: IntersectionObserverCallback) {}
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] { return []; }
}

export function installIntersectionObserverStub(): void {
  (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserverStub })
    .IntersectionObserver = IntersectionObserverStub;
}
