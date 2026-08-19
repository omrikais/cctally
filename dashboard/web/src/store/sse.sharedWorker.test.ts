import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  bootstrapErrorMessage,
  closeSSE,
  connectionState,
  FIRST_FRAME_TIMEOUT_MS,
  isBootstrapError,
  startSSE,
  _resetForTests as resetSSE,
} from './sse';
import { getState, _resetForTests as resetStore } from './store';
import type { Envelope } from '../types/envelope';
import { SHARED_READY_TIMEOUT_MS } from './dashboardStreamTransport';

function envelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-08-18T00:00:00Z',
    header: { used_pct: 42 },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks: { rows: [] },
    daily: { rows: [] },
    sessions: { rows: [] },
    projects: null,
    display: {},
    alerts: [],
    alerts_settings: {},
  } as unknown as Envelope;
}

class FakePort {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onmessageerror: (() => void) | null = null;
  sent: unknown[] = [];
  start = vi.fn();
  postMessage(value: unknown) { this.sent.push(value); }
  emit(value: unknown) { this.onmessage?.({ data: value } as MessageEvent); }
}

class FakeSharedWorker {
  static instances: FakeSharedWorker[] = [];
  port = new FakePort();
  onerror: (() => void) | null = null;
  constructor(public url: URL, public options: object) {
    FakeSharedWorker.instances.push(this);
  }
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: MessageEvent) => void>();
  constructor(public url: string) { FakeEventSource.instances.push(this); }
  addEventListener(name: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(name, listener);
  }
  close() {}
}

beforeEach(() => {
  localStorage.clear();
  resetStore();
  resetSSE();
  FakeSharedWorker.instances = [];
  FakeEventSource.instances = [];
  vi.stubGlobal('SharedWorker', FakeSharedWorker);
  vi.stubGlobal('EventSource', FakeEventSource);
  Object.defineProperty(document, 'visibilityState', {
    configurable: true, get: () => 'visible',
  });
});

afterEach(() => {
  closeSSE();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('SSE SharedWorker transport', () => {
  it('uses the shared worker and accepts its already-parsed snapshot', () => {
    startSSE();

    expect(FakeSharedWorker.instances).toHaveLength(1);
    expect(FakeEventSource.instances).toHaveLength(0);
    const worker = FakeSharedWorker.instances[0];
    expect(worker.port.sent).toContainEqual({
      version: 1, type: 'subscribe', generation: 1,
    });

    worker.port.emit({ version: 1, type: 'ready', generation: 1 });
    worker.port.emit({
      version: 1, type: 'snapshot', generation: 1,
      deliveryGeneration: 1, snapshot: envelope(),
    });
    expect(getState().snapshot?.header.used_pct).toBe(42);
  });

  it.each([
    { generated_at: '2026-08-18T00:00:00Z' },
    { envelope_version: 2, generated_at: '' },
    { envelope_version: 2, generated_at: 'invalid' },
  ])('does not connect or mutate the store for malformed snapshot payload %#', (snapshot) => {
    const onConnect = vi.fn();
    startSSE({ onConnect });
    const worker = FakeSharedWorker.instances[0];
    worker.port.emit({ version: 1, type: 'ready', generation: 1 });
    worker.port.emit({
      version: 1, type: 'snapshot', generation: 1,
      deliveryGeneration: 1, snapshot,
    });

    expect(getState().snapshot).toBeNull();
    expect(onConnect).not.toHaveBeenCalled();
    expect(connectionState()).toBe('disconnected');
  });

  it('falls back to a direct EventSource when SharedWorker bootstrap throws', () => {
    vi.stubGlobal('SharedWorker', class {
      constructor() { throw new Error('worker blocked'); }
    });

    startSSE();

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe('/api/events');
  });

  it('starts the first-frame watchdog only after the shared transport is ready', () => {
    vi.useFakeTimers();
    startSSE();
    const worker = FakeSharedWorker.instances[0];

    vi.advanceTimersByTime(SHARED_READY_TIMEOUT_MS - 1);
    expect(isBootstrapError()).toBe(false);

    worker.port.emit({ version: 1, type: 'ready', generation: 1 });
    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS);
    expect(isBootstrapError()).toBe(true);
    expect(bootstrapErrorMessage()).toMatch(/stream/i);
    vi.useRealTimers();
  });
});
