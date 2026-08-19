import type { Envelope } from '../types/envelope';
import type {
  DashboardStreamClientMessage,
  DashboardStreamWorkerMessage,
} from './dashboardStreamProtocol';
import {
  DASHBOARD_STREAM_PROTOCOL_VERSION,
  isDashboardStreamWorkerMessage,
  isEnvelope,
} from './dashboardStreamProtocol';

export const SHARED_READY_TIMEOUT_MS = 1_000;

export interface DashboardStreamTransport {
  suspend(): void;
  resume(): void;
  close(): void;
}

export interface DashboardStreamCallbacks {
  onReady(): void;
  onSnapshot(snapshot: Envelope): boolean;
  onError(): void;
}

function directTransport(callbacks: DashboardStreamCallbacks): DashboardStreamTransport {
  let source: EventSource | null = null;
  let closed = false;
  let generation = 0;

  const open = () => {
    if (closed || source != null) return;
    generation += 1;
    const myGeneration = generation;
    try {
      const next = new EventSource('/api/events');
      source = next;
      next.addEventListener('update', (event: MessageEvent<string>) => {
        if (closed || source !== next || generation !== myGeneration) return;
        try {
          const parsed = JSON.parse(event.data);
          if (!isEnvelope(parsed)) throw new Error('invalid dashboard envelope');
          callbacks.onSnapshot(parsed);
        } catch {
          console.error('Dashboard stream frame was rejected.');
          callbacks.onError();
        }
      });
      next.onerror = () => {
        if (!closed && source === next && generation === myGeneration) callbacks.onError();
      };
      callbacks.onReady();
    } catch {
      source = null;
      callbacks.onError();
    }
  };

  const suspend = () => {
    generation += 1;
    source?.close();
    source = null;
  };

  function onPageHide(event: PageTransitionEvent): void {
    if (event.persisted) suspend();
    else close();
  }

  function onPageShow(event: PageTransitionEvent): void {
    if (event.persisted) open();
  }

  function close(): void {
    if (closed) return;
    closed = true;
    if (typeof window !== 'undefined') {
      window.removeEventListener('pagehide', onPageHide);
      window.removeEventListener('pageshow', onPageShow);
    }
    generation += 1;
    source?.close();
    source = null;
  }

  open();
  if (typeof window !== 'undefined') {
    window.addEventListener('pagehide', onPageHide);
    window.addEventListener('pageshow', onPageShow);
  }
  return {
    suspend,
    resume: open,
    close,
  };
}

export function createDashboardStreamTransport(
  callbacks: DashboardStreamCallbacks,
): DashboardStreamTransport {
  if (typeof SharedWorker === 'undefined') return directTransport(callbacks);

  let worker: SharedWorker | null = null;
  let port: MessagePort | null = null;
  let fallback: DashboardStreamTransport | null = null;
  let closed = false;
  let active = true;
  let generation = 1;
  let ready = false;
  let healthy = false;
  let readyTimer: ReturnType<typeof setTimeout> | null = null;

  const clearReadyTimer = () => {
    if (readyTimer != null) clearTimeout(readyTimer);
    readyTimer = null;
  };
  const command = (type: DashboardStreamClientMessage['type']) => ({
    version: DASHBOARD_STREAM_PROTOCOL_VERSION,
    type,
    generation,
  }) as DashboardStreamClientMessage;
  const disposePort = () => {
    clearReadyTimer();
    if (port != null) {
      try { port.postMessage(command('unsubscribe')); } catch { /* best effort */ }
      port.onmessage = null;
      port.onmessageerror = null;
      try { port.close(); } catch { /* best effort */ }
    }
    if (worker != null) worker.onerror = null;
    port = null;
    worker = null;
  };
  const fallBackToDirect = () => {
    if (closed || fallback != null) return;
    if (healthy) {
      callbacks.onError();
      return;
    }
    generation += 1;
    disposePort();
    fallback = directTransport(callbacks);
  };
  const armReadyTimer = () => {
    clearReadyTimer();
    readyTimer = setTimeout(() => {
      readyTimer = null;
      if (closed || fallback != null || ready) return;
      if (healthy) callbacks.onError();
      else fallBackToDirect();
    }, SHARED_READY_TIMEOUT_MS);
  };
  const rejectMessage = () => {
    console.error('Dashboard SharedWorker message was rejected.');
    if (healthy) callbacks.onError();
    else fallBackToDirect();
  };

  try {
    worker = new SharedWorker(
      new URL('../workers/dashboardStream.shared-worker.ts', import.meta.url),
      { type: 'module', name: 'cctally-dashboard-stream-v1' },
    );
    port = worker.port;
    port.onmessage = (event: MessageEvent<unknown>) => {
      if (closed || fallback != null) return;
      if (!isDashboardStreamWorkerMessage(event.data)) {
        rejectMessage();
        return;
      }
      const message: DashboardStreamWorkerMessage = event.data;
      if (message.generation !== generation) return;
      if (message.type === 'ready') {
        ready = true;
        clearReadyTimer();
        callbacks.onReady();
        return;
      }
      if (message.type === 'stream_error') {
        if (healthy) callbacks.onError();
        else fallBackToDirect();
        return;
      }
      if (!ready) {
        rejectMessage();
        return;
      }
      if (callbacks.onSnapshot(message.snapshot)) healthy = true;
    };
    port.onmessageerror = fallBackToDirect;
    worker.onerror = fallBackToDirect;
    port.start();
    port.postMessage(command('subscribe'));
    armReadyTimer();
  } catch (err) {
    console.warn('SharedWorker dashboard stream unavailable; using a direct stream.', err);
    disposePort();
    fallback = directTransport(callbacks);
  }

  function suspend(): void {
    if (closed || !active) return;
    active = false;
    fallback?.suspend();
    if (port != null) {
      generation += 1;
      ready = false;
      clearReadyTimer();
      port.postMessage(command('suspend'));
    }
  }

  function resume(): void {
    if (closed || active) return;
    active = true;
    if (fallback != null) {
      fallback.resume();
      return;
    }
    if (port != null) {
      generation += 1;
      ready = false;
      port.postMessage(command('resume'));
      armReadyTimer();
    }
  }

  function onPageHide(event: PageTransitionEvent): void {
    if (event.persisted) suspend();
    else close();
  }

  function onPageShow(event: PageTransitionEvent): void {
    if (event.persisted) resume();
  }

  function close(): void {
    if (closed) return;
    closed = true;
    if (typeof window !== 'undefined') {
      window.removeEventListener('pagehide', onPageHide);
      window.removeEventListener('pageshow', onPageShow);
    }
    fallback?.close();
    generation += 1;
    disposePort();
  }

  if (typeof window !== 'undefined') {
    window.addEventListener('pagehide', onPageHide);
    window.addEventListener('pageshow', onPageShow);
  }

  return {
    suspend,
    resume,
    close,
  };
}
