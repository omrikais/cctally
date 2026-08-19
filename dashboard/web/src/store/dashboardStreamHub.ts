import type { Envelope } from '../types/envelope';
import type {
  DashboardStreamClientMessage,
  DashboardStreamWorkerMessage,
} from './dashboardStreamProtocol';
import {
  DASHBOARD_STREAM_PROTOCOL_VERSION,
  isDashboardStreamClientMessage,
  isEnvelope,
} from './dashboardStreamProtocol';

interface StreamPort {
  onmessage: ((event: MessageEvent<DashboardStreamClientMessage>) => void) | null;
  postMessage(value: DashboardStreamWorkerMessage): void;
  start(): void;
}

interface StreamEventSource {
  onerror: ((event: Event) => void) | null;
  addEventListener(name: 'update', listener: (event: MessageEvent<string>) => void): void;
  close(): void;
}

interface PortState {
  port: StreamPort;
  active: boolean;
  generation: number;
}

/** Owns one server stream for every active tab connected to a SharedWorker. */
export class DashboardStreamHub {
  private readonly ports = new Map<StreamPort, PortState>();
  private stream: StreamEventSource | null = null;
  private lastSnapshot: { snapshot: Envelope; deliveryGeneration: number } | null = null;
  private deliveryGeneration = 0;

  constructor(
    private readonly createEventSource: () => StreamEventSource,
    private readonly parse: (text: string) => unknown = JSON.parse,
  ) {}

  connect(port: StreamPort): void {
    this.ports.set(port, { port, active: false, generation: 0 });
    port.onmessage = (event) => this.onPortMessage(port, event.data);
    try {
      port.start();
    } catch {
      this.ports.delete(port);
    }
  }

  private onPortMessage(port: StreamPort, value: unknown): void {
    const state = this.ports.get(port);
    if (state == null || !isDashboardStreamClientMessage(value)) return;
    const message: DashboardStreamClientMessage = value;
    if (message.generation <= state.generation) return;
    state.generation = message.generation;
    if (message.type === 'unsubscribe') {
      this.ports.delete(port);
      this.reconcileStream();
      return;
    }
    state.active = message.type === 'subscribe' || message.type === 'resume';
    this.reconcileStream();
    if (state.active && this.stream != null) {
      state.port.postMessage({
        version: DASHBOARD_STREAM_PROTOCOL_VERSION,
        type: 'ready',
        generation: state.generation,
      });
      if (this.lastSnapshot != null) {
        state.port.postMessage({
          version: DASHBOARD_STREAM_PROTOCOL_VERSION,
          type: 'snapshot',
          generation: state.generation,
          deliveryGeneration: this.lastSnapshot.deliveryGeneration,
          snapshot: this.lastSnapshot.snapshot,
        });
      }
    }
  }

  private reconcileStream(): void {
    const hasActivePort = [...this.ports.values()].some((state) => state.active);
    if (hasActivePort && this.stream == null) this.openStream();
    if (!hasActivePort && this.stream != null) {
      this.stream.close();
      this.stream = null;
    }
  }

  private openStream(): void {
    let stream: StreamEventSource;
    try {
      stream = this.createEventSource();
    } catch {
      this.broadcastError();
      return;
    }
    this.stream = stream;
    stream.addEventListener('update', (event) => {
      if (this.stream !== stream) return;
      try {
        const parsed = this.parse(event.data);
        if (!isEnvelope(parsed)) throw new Error('invalid dashboard envelope');
        const snapshot = parsed;
        this.deliveryGeneration += 1;
        this.lastSnapshot = { snapshot, deliveryGeneration: this.deliveryGeneration };
        for (const state of this.ports.values()) {
          if (!state.active) continue;
          state.port.postMessage({
            version: DASHBOARD_STREAM_PROTOCOL_VERSION,
            type: 'snapshot',
            generation: state.generation,
            deliveryGeneration: this.deliveryGeneration,
            snapshot,
          });
        }
      } catch (err) {
        console.error('Dashboard stream frame was rejected.');
      }
    });
    stream.onerror = () => {
      if (this.stream !== stream) return;
      this.broadcastError();
    };
  }

  private broadcastError(): void {
    for (const state of this.ports.values()) {
      if (!state.active) continue;
      const message: DashboardStreamWorkerMessage = {
        version: DASHBOARD_STREAM_PROTOCOL_VERSION,
        type: 'stream_error',
        generation: state.generation,
      };
      state.port.postMessage(message);
    }
  }
}
