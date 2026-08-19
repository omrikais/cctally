import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DashboardStreamHub } from './dashboardStreamHub';
import {
  createDashboardStreamTransport,
  SHARED_READY_TIMEOUT_MS,
} from './dashboardStreamTransport';

class FakePort {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onmessageerror: (() => void) | null = null;
  sent: unknown[] = [];
  closed = false;
  startError: Error | null = null;
  postMessage(value: unknown) { this.sent.push(value); }
  start() {
    if (this.startError != null) throw this.startError;
  }
  close() { this.closed = true; }
  clientMessage(value: unknown) {
    this.onmessage?.({ data: value } as MessageEvent);
  }
  workerMessage(value: unknown) {
    this.onmessage?.({ data: value } as MessageEvent);
  }
}

class FakeEventSource {
  closed = false;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: MessageEvent) => void>();
  addEventListener(name: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(name, listener);
  }
  close() { this.closed = true; }
  update(data: string) {
    this.listeners.get('update')?.({ data } as MessageEvent);
  }
}

const validSnapshot = {
  envelope_version: 2,
  generated_at: '2026-08-18T00:00:00Z',
  header: {},
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
} as const;

function pageTransition(type: 'pagehide' | 'pageshow', persisted: boolean): Event {
  const event = new Event(type);
  Object.defineProperty(event, 'persisted', { value: persisted });
  return event;
}

describe('DashboardStreamHub', () => {
  it('opens one EventSource, parses once, and clones the snapshot to active tabs', () => {
    const sources: FakeEventSource[] = [];
    const parse = vi.fn(JSON.parse);
    const hub = new DashboardStreamHub(
      () => {
        const source = new FakeEventSource();
        sources.push(source);
        return source;
      },
      parse,
    );
    const first = new FakePort();
    const second = new FakePort();
    hub.connect(first);
    hub.connect(second);

    first.clientMessage({ version: 1, type: 'subscribe', generation: 1 });
    second.clientMessage({ version: 1, type: 'subscribe', generation: 1 });
    expect(sources).toHaveLength(1);
    expect(first.sent).toEqual([{ version: 1, type: 'ready', generation: 1 }]);
    expect(second.sent).toEqual([{ version: 1, type: 'ready', generation: 1 }]);

    sources[0].update(JSON.stringify(validSnapshot));

    expect(parse).toHaveBeenCalledTimes(1);
    expect(first.sent.at(-1)).toEqual({
      version: 1,
      type: 'snapshot',
      generation: 1,
      deliveryGeneration: 1,
      snapshot: validSnapshot,
    });
    expect(second.sent).toEqual(first.sent);
  });

  it('keeps the shared stream for another active tab and closes it after the last leaves', () => {
    const sources: FakeEventSource[] = [];
    const hub = new DashboardStreamHub(() => {
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    });
    const first = new FakePort();
    const second = new FakePort();
    hub.connect(first);
    hub.connect(second);
    first.clientMessage({ version: 1, type: 'subscribe', generation: 1 });
    second.clientMessage({ version: 1, type: 'subscribe', generation: 1 });

    first.clientMessage({ version: 1, type: 'suspend', generation: 2 });
    expect(sources[0].closed).toBe(false);
    second.clientMessage({ version: 1, type: 'unsubscribe', generation: 2 });
    expect(sources[0].closed).toBe(true);
  });

  it('seeds a tab that joins after the shared stream already received a snapshot', () => {
    const sources: FakeEventSource[] = [];
    const hub = new DashboardStreamHub(() => {
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    });
    const first = new FakePort();
    hub.connect(first);
    first.clientMessage({ version: 1, type: 'subscribe', generation: 1 });
    sources[0].update(JSON.stringify(validSnapshot));

    const late = new FakePort();
    hub.connect(late);
    late.clientMessage({ version: 1, type: 'subscribe', generation: 1 });

    expect(sources).toHaveLength(1);
    expect(late.sent).toEqual([
      { version: 1, type: 'ready', generation: 1 },
      {
        version: 1,
        type: 'snapshot',
        generation: 1,
        deliveryGeneration: 1,
        snapshot: validSnapshot,
      },
    ]);
  });

  it('ignores stale per-port commands and tags a resumed seed with the current generation', () => {
    const sources: FakeEventSource[] = [];
    const hub = new DashboardStreamHub(() => {
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    });
    const port = new FakePort();
    hub.connect(port);
    port.clientMessage({ version: 1, type: 'subscribe', generation: 4 });
    sources[0].update(JSON.stringify(validSnapshot));
    port.clientMessage({ version: 1, type: 'suspend', generation: 3 });
    expect(sources[0].closed).toBe(false);

    port.clientMessage({ version: 1, type: 'suspend', generation: 5 });
    expect(sources[0].closed).toBe(true);
    port.clientMessage({ version: 1, type: 'resume', generation: 6 });

    expect(port.sent.at(-1)).toEqual({
      version: 1,
      type: 'snapshot',
      generation: 6,
      deliveryGeneration: 1,
      snapshot: validSnapshot,
    });
  });
});

class FakeSharedWorker {
  static instances: FakeSharedWorker[] = [];
  port = new FakePort();
  onerror: (() => void) | null = null;
  constructor() { FakeSharedWorker.instances.push(this); }
}

class FakeDirectEventSource extends FakeEventSource {
  static instances: FakeDirectEventSource[] = [];
  constructor(public url: string) {
    super();
    FakeDirectEventSource.instances.push(this);
  }
}

describe('createDashboardStreamTransport', () => {
  const acceptedSnapshot = validSnapshot as never;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeSharedWorker.instances = [];
    FakeDirectEventSource.instances = [];
    vi.stubGlobal('SharedWorker', FakeSharedWorker);
    vi.stubGlobal('EventSource', FakeDirectEventSource);
  });

  afterEach(() => {
    window.dispatchEvent(pageTransition('pagehide', false));
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function callbacks(accept = true) {
    return {
      onReady: vi.fn(),
      onSnapshot: vi.fn(() => accept),
      onError: vi.fn(),
    };
  }

  it('falls back once when no versioned ready arrives and ignores stale worker delivery', () => {
    const cb = callbacks();
    createDashboardStreamTransport(cb);
    const worker = FakeSharedWorker.instances[0];

    expect(worker.port.sent).toEqual([
      { version: 1, type: 'subscribe', generation: 1 },
    ]);
    vi.advanceTimersByTime(SHARED_READY_TIMEOUT_MS);

    expect(FakeDirectEventSource.instances).toHaveLength(1);
    expect(worker.port.closed).toBe(true);
    expect(worker.port.sent).toContainEqual({
      version: 1, type: 'unsubscribe', generation: 2,
    });
    worker.port.workerMessage({
      version: 1, type: 'snapshot', generation: 1,
      deliveryGeneration: 1, snapshot: acceptedSnapshot,
    });
    expect(cb.onSnapshot).not.toHaveBeenCalled();
  });

  it('falls back once when starting the port throws', () => {
    class StartFailureWorker extends FakeSharedWorker {
      constructor() {
        super();
        this.port.startError = new Error('port start failed');
      }
    }
    vi.stubGlobal('SharedWorker', StartFailureWorker);

    createDashboardStreamTransport(callbacks());

    expect(FakeDirectEventSource.instances).toHaveLength(1);
    expect(FakeSharedWorker.instances[0].port.closed).toBe(true);
  });

  it('falls back once on a worker bootstrap error before health', () => {
    const cb = callbacks();
    createDashboardStreamTransport(cb);
    const worker = FakeSharedWorker.instances[0];

    worker.onerror?.();
    worker.onerror?.();

    expect(FakeDirectEventSource.instances).toHaveLength(1);
    expect(worker.port.closed).toBe(true);
  });

  it('never opens a direct stream after an accepted shared snapshot', () => {
    const cb = callbacks();
    createDashboardStreamTransport(cb);
    const worker = FakeSharedWorker.instances[0];
    worker.port.workerMessage({ version: 1, type: 'ready', generation: 1 });
    worker.port.workerMessage({
      version: 1, type: 'snapshot', generation: 1,
      deliveryGeneration: 1, snapshot: acceptedSnapshot,
    });
    worker.onerror?.();
    worker.port.workerMessage({ version: 1, type: 'stream_error', generation: 1 });

    expect(cb.onSnapshot).toHaveBeenCalledTimes(1);
    expect(cb.onError).toHaveBeenCalled();
    expect(FakeDirectEventSource.instances).toHaveLength(0);
  });

  it('rejects stale-generation worker messages', () => {
    const cb = callbacks();
    const transport = createDashboardStreamTransport(cb);
    const worker = FakeSharedWorker.instances[0];
    worker.port.workerMessage({ version: 1, type: 'ready', generation: 1 });
    transport.suspend();
    transport.resume();
    worker.port.workerMessage({
      version: 1, type: 'snapshot', generation: 1,
      deliveryGeneration: 1, snapshot: acceptedSnapshot,
    });
    worker.port.workerMessage({ version: 1, type: 'ready', generation: 3 });
    worker.port.workerMessage({
      version: 1, type: 'snapshot', generation: 3,
      deliveryGeneration: 2, snapshot: acceptedSnapshot,
    });

    expect(cb.onSnapshot).toHaveBeenCalledTimes(1);
  });

  it('fails closed on a malformed worker message', () => {
    const cb = callbacks();
    createDashboardStreamTransport(cb);
    const worker = FakeSharedWorker.instances[0];
    worker.port.workerMessage({ type: 'snapshot', snapshot: acceptedSnapshot });

    expect(cb.onSnapshot).not.toHaveBeenCalled();
    expect(FakeDirectEventSource.instances).toHaveLength(1);
    expect(worker.port.closed).toBe(true);
  });

  it.each([
    { generated_at: '2026-08-18T00:00:00Z' },
    { envelope_version: 2, generated_at: '' },
    { envelope_version: 2, generated_at: 'invalid' },
  ])('fails closed on a malformed snapshot payload %#', (snapshot) => {
    const cb = callbacks();
    createDashboardStreamTransport(cb);
    const worker = FakeSharedWorker.instances[0];
    worker.port.workerMessage({ version: 1, type: 'ready', generation: 1 });
    worker.port.workerMessage({
      version: 1, type: 'snapshot', generation: 1,
      deliveryGeneration: 1, snapshot,
    });

    expect(cb.onSnapshot).not.toHaveBeenCalled();
    expect(cb.onError).not.toHaveBeenCalled();
    expect(FakeDirectEventSource.instances).toHaveLength(1);
  });

  it('suspends for BFCache and resumes on restore without discarding the port', () => {
    createDashboardStreamTransport(callbacks());
    const worker = FakeSharedWorker.instances[0];

    window.dispatchEvent(pageTransition('pagehide', true));

    expect(worker.port.closed).toBe(false);
    expect(worker.port.sent).toContainEqual({
      version: 1, type: 'suspend', generation: 2,
    });

    window.dispatchEvent(pageTransition('pageshow', true));

    expect(worker.port.closed).toBe(false);
    expect(worker.port.sent).toContainEqual({
      version: 1, type: 'resume', generation: 3,
    });
  });

  it('suspends and restores a direct fallback across BFCache', () => {
    vi.stubGlobal('SharedWorker', undefined);
    createDashboardStreamTransport(callbacks());

    window.dispatchEvent(pageTransition('pagehide', true));
    expect(FakeDirectEventSource.instances[0].closed).toBe(true);

    window.dispatchEvent(pageTransition('pageshow', true));
    expect(FakeDirectEventSource.instances).toHaveLength(2);
    expect(FakeDirectEventSource.instances[1].closed).toBe(false);
  });

  it('best-effort unsubscribes the port when the page is truly discarded', () => {
    createDashboardStreamTransport(callbacks());
    const worker = FakeSharedWorker.instances[0];

    window.dispatchEvent(pageTransition('pagehide', false));

    expect(worker.port.closed).toBe(true);
    expect(worker.port.sent).toContainEqual({
      version: 1, type: 'unsubscribe', generation: 2,
    });
  });
});
