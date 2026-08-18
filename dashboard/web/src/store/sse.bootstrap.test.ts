import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  startSSE, closeSSE, isBootstrapError, _resetForTests as _resetSSE,
} from './sse';
import { _resetForTests as _resetStore, getState } from './store';
import type { Envelope } from '../types/envelope';

function minimalEnvelope(genAt: string): Envelope {
  return {
    envelope_version: 2, generated_at: genAt, last_sync_at: null,
    sync_age_s: null, last_sync_error: null,
    header: { week_label: null, used_pct: null, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] }, projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [], alerts_settings: { enabled: true, weekly_thresholds: [],
      five_hour_thresholds: [], budget_thresholds: [] },
  } as unknown as Envelope;
}

// Capturable fake EventSource — tests push 'update' events at will.
class FakeES {
  static last: FakeES | null = null;
  onerror: (() => void) | null = null;
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  constructor(public url: string) { FakeES.last = this; }
  addEventListener(t: string, fn: (ev: MessageEvent) => void) {
    (this.listeners[t] ||= []).push(fn);
  }
  close() {}
  emitUpdate(env: Envelope) {
    const ev = { data: JSON.stringify(env) } as MessageEvent;
    (this.listeners['update'] || []).forEach((fn) => fn(ev));
  }
  emitError() { this.onerror?.(); }
}

beforeEach(() => {
  localStorage.clear();
  _resetStore();
  _resetSSE();
  vi.stubGlobal('EventSource', FakeES as unknown as typeof EventSource);
  FakeES.last = null;
});
afterEach(() => { closeSSE(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

// #583 S3 §7 — the bootstrap `fetch('/api/data')` is deleted, so the cold-start
// error view is now raised by the STREAM rather than by a rejected fetch. The
// condition is unchanged in substance: no snapshot has landed from any source.
describe('sse bootstrapError lifecycle (B2/B3, restated on the stream)', () => {
  it('raises bootstrapError when the stream errors before any snapshot landed', () => {
    startSSE();
    expect(getState().snapshot).toBeNull();
    expect(isBootstrapError()).toBe(false);
    FakeES.last!.emitError();
    expect(getState().snapshot).toBeNull();
    expect(isBootstrapError()).toBe(true);
  });

  it('clears bootstrapError once an SSE update applies a snapshot', () => {
    startSSE();
    FakeES.last!.emitError();
    expect(isBootstrapError()).toBe(true);
    FakeES.last!.emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));
    expect(isBootstrapError()).toBe(false);
    expect(getState().snapshot).not.toBeNull();
  });

  it('a stream error AFTER a snapshot landed does NOT raise the error view', () => {
    startSSE();
    FakeES.last!.emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));
    expect(getState().snapshot).not.toBeNull();
    // A transient drop is `disconnected`, which is a different view: the
    // cold-start error means "no data at all", and there is data.
    FakeES.last!.emitError();
    expect(isBootstrapError()).toBe(false);
  });

  it('a late error from a SUPERSEDED stream does NOT raise the error view', () => {
    startSSE();
    const superseded = FakeES.last!;
    startSSE();                       // a second start invalidates the first
    FakeES.last!.emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));
    expect(getState().snapshot).not.toBeNull();
    // The old EventSource can still fire: it was closed, but a callback may
    // already be queued. It belongs to a dead generation and must be ignored.
    superseded.emitError();
    expect(isBootstrapError()).toBe(false);
  });

  it('never fetches /api/data', () => {
    const spy = vi.fn();
    vi.stubGlobal('fetch', spy);
    startSSE();
    FakeES.last!.emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));
    expect(spy).not.toHaveBeenCalled();
  });
});

// #583 S3 §7/§8 — the generation guard, on the UPDATE listener rather than on
// the error handler. Both callbacks capture `startGeneration` at `openStream`
// time; the error half is covered above, and the half that can actually repaint
// the board was not covered at all.
describe('a dead generation cannot write into the store', () => {
  it('drops an update event delivered by a SUPERSEDED stream', () => {
    startSSE();
    const superseded = FakeES.last!;
    startSSE();                       // a second start invalidates the first
    expect(getState().snapshot).toBeNull();

    // The old EventSource was closed, but a callback may already be queued.
    superseded.emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));

    expect(getState().snapshot).toBeNull();
  });

  it('drops an update event delivered by a stream closeSSE tore down', () => {
    startSSE();
    const torn = FakeES.last!;
    closeSSE();

    // `closeSSE()` is a deliberate teardown: the caller has decided this client
    // is finished. A callback queued before the close must not repaint the
    // store afterwards, exactly as a suspended stream's must not. Note that
    // `closeSSE()` also calls `resetSnapshotOrdering()`, so the ordering guard
    // would NOT have rejected this frame — only the generation can.
    torn.emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));

    expect(getState().snapshot).toBeNull();
  });
});
