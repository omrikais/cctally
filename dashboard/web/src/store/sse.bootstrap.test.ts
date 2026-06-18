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

describe('sse bootstrapError lifecycle (B2/B3)', () => {
  it('sets bootstrapError when the bootstrap fetch rejects and no snapshot landed', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('boom')));
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    expect(getState().snapshot).toBeNull();
    expect(isBootstrapError()).toBe(true);
  });

  it('clears bootstrapError once an SSE update applies a snapshot', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('boom')));
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    expect(isBootstrapError()).toBe(true);
    FakeES.last!.emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));
    expect(isBootstrapError()).toBe(false);
    expect(getState().snapshot).not.toBeNull();
  });

  it('a late bootstrap reject AFTER a snapshot landed does NOT raise the error', async () => {
    // fetch rejects, but only after we let an update land first.
    let rejectFetch: (e: Error) => void = () => {};
    const pending = new Promise((_res, rej) => { rejectFetch = rej; });
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending));
    startSSE();
    // SSE update lands before the bootstrap settles.
    FakeES.last!.emitUpdate(minimalEnvelope('2026-04-20T12:00:00Z'));
    expect(getState().snapshot).not.toBeNull();
    // Now the bootstrap fetch finally rejects.
    rejectFetch(new Error('late'));
    await Promise.resolve(); await Promise.resolve();
    expect(isBootstrapError()).toBe(false);
  });
});
