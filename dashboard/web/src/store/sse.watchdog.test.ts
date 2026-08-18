// #583 S3 §7 — the first-frame watchdog.
//
// The bootstrap `fetch('/api/data')` is gone, so the EventSource is the only
// path to a first paint and `es.onerror` is the only thing that raises the
// cold-start error view. A connection that is QUEUED rather than failed never
// fires `onerror`: the browser's six-connections-per-origin HTTP/1.1 limit with
// several tabs open, an intermediary that buffers `text/event-stream`, and a
// server that accepts the socket and then stalls all produce a stream that is
// open and silent. Before this session those situations still painted from
// `/api/data` and merely stopped updating; now they render the skeleton grid
// forever with no diagnostic.
//
// The watchdog bounds that wait. A healthy subscribe is seeded by the hub
// immediately, so a real connection delivers its first frame in milliseconds
// and ten seconds is far outside normal.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  startSSE, closeSSE, isBootstrapError, bootstrapErrorMessage,
  FIRST_FRAME_TIMEOUT_MS, _resetForTests as _resetSSE,
} from './sse';
import { _resetForTests as _resetStore, getState } from './store';
import type { Envelope } from '../types/envelope';

function minimalEnvelope(genAt: string): Envelope {
  return {
    envelope_version: 2, generated_at: genAt, last_sync_at: null,
    sync_age_s: null, last_sync_error: null,
    header: {
      week_label: null, used_pct: null, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] }, projects: null,
    display: {
      tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC',
      offset_seconds: 0,
    },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [],
      budget_thresholds: [],
    },
  } as unknown as Envelope;
}

class FakeES {
  static instances: FakeES[] = [];
  closed = false;
  onerror: (() => void) | null = null;
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  constructor(public url: string) { FakeES.instances.push(this); }
  addEventListener(t: string, fn: (ev: MessageEvent) => void) {
    (this.listeners[t] ||= []).push(fn);
  }
  removeEventListener() {}
  close() { this.closed = true; }
  emitUpdate(env: Envelope) {
    const ev = { data: JSON.stringify(env) } as MessageEvent;
    (this.listeners['update'] || []).forEach((fn) => fn(ev));
  }
}

function mainStreams(): FakeES[] {
  return FakeES.instances.filter((es) => es.url === '/api/events');
}

function currentMain(): FakeES {
  const all = mainStreams();
  return all[all.length - 1];
}

function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  FakeES.instances = [];
  _resetStore();
  _resetSSE();
  vi.stubGlobal('EventSource', FakeES as unknown as typeof EventSource);
  setVisibility('visible');
});

afterEach(() => {
  closeSSE();
  setVisibility('visible');
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('#583 S3 §7 — the first-frame watchdog', () => {
  it('raises the error view when an opened stream delivers no first frame', () => {
    startSSE();
    expect(mainStreams()).toHaveLength(1);
    expect(isBootstrapError()).toBe(false);

    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS);

    expect(isBootstrapError()).toBe(true);
    // The message must name the STREAM as the thing that did not deliver.
    // "Couldn't load dashboard data" alone sends the reader to the server,
    // which in this failure mode is answering perfectly well.
    expect(bootstrapErrorMessage()).toMatch(/stream/i);
  });

  it('does not raise it before the timeout has elapsed', () => {
    // Discriminator against a zero-length or missing delay: a watchdog that
    // fires immediately would make the whole cold-start path an error view.
    startSSE();
    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS - 1);
    expect(isBootstrapError()).toBe(false);
  });

  it('the first accepted snapshot disarms it', () => {
    startSSE();
    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS - 1);
    currentMain().emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));
    expect(getState().snapshot).not.toBeNull();

    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS * 10);

    expect(isBootstrapError()).toBe(false);
    expect(bootstrapErrorMessage()).toBeNull();
  });

  it('closeSSE disarms it', () => {
    startSSE();
    closeSSE();

    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS * 10);

    // A deliberate teardown has no first frame to wait for, and raising the
    // error view over a torn-down client would repaint a dashboard nobody
    // asked to keep alive.
    expect(isBootstrapError()).toBe(false);
    expect(bootstrapErrorMessage()).toBeNull();
  });

  it('arms no watchdog for a page restored into an already-hidden tab', () => {
    // §8 skips opening the stream entirely in this case, so there is no first
    // frame to wait for. Arming here would raise the error view against a tab
    // that is deliberately not streaming, and the user would meet it on return.
    setVisibility('hidden');
    startSSE();
    expect(mainStreams()).toHaveLength(0);

    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS * 10);

    expect(isBootstrapError()).toBe(false);
    expect(mainStreams()).toHaveLength(0);

    // And the watchdog arms normally once the tab returns and a stream opens.
    setVisibility('visible');
    expect(mainStreams()).toHaveLength(1);
    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS);
    expect(isBootstrapError()).toBe(true);
  });

  it('does not raise the error view on a reconnect that already has data', () => {
    startSSE();
    currentMain().emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));
    // A resume opens a fresh stream and arms a fresh watchdog. The client is
    // not without data, so a silent reopen is a `disconnected` overlay at
    // worst — never the cold-start error view, which claims there is nothing
    // to show.
    setVisibility('hidden');
    vi.advanceTimersByTime(30_000);
    setVisibility('visible');
    expect(mainStreams()).toHaveLength(2);

    vi.advanceTimersByTime(FIRST_FRAME_TIMEOUT_MS * 10);

    expect(isBootstrapError()).toBe(false);
  });
});
