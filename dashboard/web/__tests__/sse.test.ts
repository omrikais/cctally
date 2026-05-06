import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { startSSE, isDisconnected, closeSSE, _resetForTests as _resetSSE } from '../src/store/sse';
import { getState, _resetForTests as _resetStore } from '../src/store/store';

// Minimal EventSource mock
class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  readyState = 0;
  onerror: ((ev: Event) => void) | null = null;
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  closed = false;
  constructor(url: string) { this.url = url; MockEventSource.instances.push(this); }
  addEventListener(name: string, fn: (ev: MessageEvent) => void): void {
    (this.listeners[name] ||= []).push(fn);
  }
  close(): void { this.closed = true; }
  emit(name: string, data: unknown): void {
    (this.listeners[name] || []).forEach((fn) => fn({ data: JSON.stringify(data) } as MessageEvent));
  }
  triggerError(): void { if (this.onerror) this.onerror(new Event('error')); }
}

function snap(generated_at: string, used_pct = 10) {
  return {
    envelope_version: 2, generated_at,
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: { week_label: null, used_pct, five_hour_pct: null, dollar_per_pct: null,
              forecast_pct: null, forecast_verdict: 'ok' as const, vs_last_week_delta: null },
    current_week: null, forecast: null, trend: null,
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
  };
}

beforeEach(() => {
  MockEventSource.instances = [];
  (globalThis as any).EventSource = MockEventSource;
  (globalThis as any).fetch = vi.fn().mockResolvedValue({ json: () => Promise.resolve(snap('2026-04-24T10:00:00Z', 5)) });
  _resetStore();
  _resetSSE();
  localStorage.clear();
});

afterEach(() => {
  closeSSE();
});

describe('startSSE', () => {
  it('calls fetch("/api/data") for bootstrap and feeds updateSnapshot', async () => {
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    expect((globalThis as any).fetch).toHaveBeenCalledWith('/api/data');
    expect(getState().snapshot?.header.used_pct).toBe(5);
  });

  it('opens EventSource("/api/events")', () => {
    startSSE();
    expect(MockEventSource.instances.length).toBe(1);
    expect(MockEventSource.instances[0].url).toBe('/api/events');
  });

  it('applies "update" events via updateSnapshot', () => {
    startSSE();
    MockEventSource.instances[0].emit('update', snap('2026-04-24T10:00:05Z', 42));
    expect(getState().snapshot?.header.used_pct).toBe(42);
  });

  it('is idempotent — second call closes the prior EventSource', () => {
    startSSE();
    startSSE();
    expect(MockEventSource.instances[0].closed).toBe(true);
    expect(MockEventSource.instances[1].closed).toBe(false);
    expect(MockEventSource.instances.length).toBe(2);
  });

  it('marks disconnected on error', () => {
    startSSE();
    MockEventSource.instances[0].triggerError();
    expect(isDisconnected()).toBe(true);
  });

  it('clears disconnected on next successful update', () => {
    startSSE();
    MockEventSource.instances[0].triggerError();
    expect(isDisconnected()).toBe(true);
    MockEventSource.instances[0].emit('update', snap('2026-04-24T10:00:05Z'));
    expect(isDisconnected()).toBe(false);
  });

  it('swallows a malformed SSE event without throwing', () => {
    startSSE();
    const es = MockEventSource.instances[0];
    const before = getState().snapshot;  // capture
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    // Manually craft a bad event bypassing emit()'s JSON.stringify
    (es.listeners.update || []).forEach((fn) => fn({ data: 'not json' } as MessageEvent));
    expect(spy).toHaveBeenCalled();
    expect(getState().snapshot).toBe(before);  // verify no partial update
    spy.mockRestore();
  });

  it('fires onConnect after bootstrap', async () => {
    const spy = vi.fn();
    startSSE({ onConnect: spy });
    await Promise.resolve(); await Promise.resolve();
    expect(spy).toHaveBeenCalled();
  });

  it('fires onDisconnect on error', () => {
    const spy = vi.fn();
    startSSE({ onDisconnect: spy });
    MockEventSource.instances[0].triggerError();
    expect(spy).toHaveBeenCalled();
  });

  it('does NOT fire onDisconnect on repeated onerror during a single outage', () => {
    const spy = vi.fn();
    startSSE({ onDisconnect: spy });
    MockEventSource.instances[0].triggerError();
    MockEventSource.instances[0].triggerError();
    MockEventSource.instances[0].triggerError();
    expect(spy).toHaveBeenCalledTimes(1);  // only the transition fires it
  });

  it('does NOT fire onConnect on every update — only on reconnect transition', async () => {
    const spy = vi.fn();
    startSSE({ onConnect: spy });
    await Promise.resolve(); await Promise.resolve();
    const bootstrapCalls = spy.mock.calls.length;  // 1 from bootstrap
    MockEventSource.instances[0].emit('update', snap('2026-04-24T10:00:05Z'));
    MockEventSource.instances[0].emit('update', snap('2026-04-24T10:00:06Z'));
    MockEventSource.instances[0].emit('update', snap('2026-04-24T10:00:07Z'));
    // Non-transition updates should NOT fire onConnect
    expect(spy).toHaveBeenCalledTimes(bootstrapCalls);
  });

  it('fires onConnect on reconnect transition (disconnected → connected)', () => {
    const spy = vi.fn();
    startSSE({ onConnect: spy });
    // Wait for bootstrap to settle would require await; for this test we start by triggering error
    // to force disconnected, then emit update to verify the transition fire.
    MockEventSource.instances[0].triggerError();
    const beforeReconnect = spy.mock.calls.length;
    MockEventSource.instances[0].emit('update', snap('2026-04-24T10:00:05Z'));
    expect(spy.mock.calls.length).toBe(beforeReconnect + 1);
  });
});

// Threshold-actions T15: cold-start rule wired through the SSE singleton.
// The reducer behavior is covered by RecentAlertsPanel-seenIds.test.tsx;
// these tests verify that startSSE dispatches INGEST_SNAPSHOT_ALERTS with
// the right `isFirstTick` flag on (a) bootstrap, (b) subsequent updates,
// and (c) post-reconnect after an onerror drop.
function alert(id: string) {
  return {
    id,
    axis: 'weekly' as const,
    threshold: 90,
    crossed_at: '2026-04-29T14:32:11Z',
    alerted_at: '2026-04-29T14:32:11Z',
    context: { week_start_date: '2026-04-27' },
  };
}

function snapWithAlerts(generated_at: string, alerts: ReturnType<typeof alert>[]) {
  return { ...snap(generated_at), alerts };
}

describe('startSSE — INGEST_SNAPSHOT_ALERTS wiring (T15)', () => {
  it('cold-start: bootstrap snapshot populates seenAlertIds without surfacing toast', async () => {
    (globalThis as any).fetch = vi.fn().mockResolvedValue({
      json: () => Promise.resolve(snapWithAlerts('2026-04-24T10:00:00Z', [alert('weekly:2026-04-27:90')])),
    });
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    expect(getState().seenAlertIds.has('weekly:2026-04-27:90')).toBe(true);
    expect(getState().toast).toBeNull();
  });

  it('subsequent update with new alert surfaces toast', async () => {
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    // Bootstrap consumed the cold-start tick; this update is post-cold-start.
    MockEventSource.instances[0].emit(
      'update',
      snapWithAlerts('2026-04-24T10:00:05Z', [alert('weekly:2026-04-27:95')]),
    );
    expect(getState().toast?.kind).toBe('alert');
  });

  it('reconnect after onerror re-arms cold-start (next update does not toast)', async () => {
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    // Drop connection.
    MockEventSource.instances[0].triggerError();
    expect(isDisconnected()).toBe(true);
    // First post-reconnect update: even though id is unseen, isFirstTick=true
    // means it merges into seenAlertIds without toasting.
    MockEventSource.instances[0].emit(
      'update',
      snapWithAlerts('2026-04-24T10:00:05Z', [alert('weekly:2026-04-27:90')]),
    );
    expect(getState().toast).toBeNull();
    expect(getState().seenAlertIds.has('weekly:2026-04-27:90')).toBe(true);
    // Next update after reconnect IS toasted if the id is unseen.
    MockEventSource.instances[0].emit(
      'update',
      snapWithAlerts('2026-04-24T10:00:06Z', [alert('weekly:2026-04-27:95')]),
    );
    expect(getState().toast?.kind).toBe('alert');
  });

  it('snapshot without `alerts` field defaults to [] (defensive ?? [])', async () => {
    // snap() returns an envelope with NO `alerts` field — tests legacy
    // backend / partial envelope. Should not throw, should still empty
    // out alerts in state.
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    expect(getState().alerts).toEqual([]);
    // And the dispatch did run (cold-start true → no toast either way).
    expect(getState().toast).toBeNull();
  });

  it('alerts_settings from envelope propagates to state.alertsConfig (C1)', async () => {
    // C1 regression: prior to this fix, the SSE handler dispatched the
    // alerts list but never the settings block, leaving alertsConfig
    // frozen at the hardcoded default. Now the envelope is the source
    // of truth and a server-side flip arrives on the next tick.
    const envelopeSettings = {
      enabled: true,
      weekly_thresholds: [80, 90],
      five_hour_thresholds: [85],
    };
    (globalThis as any).fetch = vi.fn().mockResolvedValue({
      json: () =>
        Promise.resolve({
          ...snap('2026-04-24T10:00:00Z'),
          alerts: [],
          alerts_settings: envelopeSettings,
        }),
    });
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    expect(getState().alertsConfig).toEqual(envelopeSettings);
  });

  it('out-of-order bootstrap is dropped — alerts/alertsConfig/seenAlertIds untouched', async () => {
    // Regression: ingestAlerts used to run unconditionally on both
    // bootstrap and SSE updates, even when updateSnapshot rejected the
    // envelope as out-of-order. A late bootstrap could replace
    // state.alerts with stale rows and pollute seenAlertIds with stale
    // ids. The fix gates ingestAlerts on updateSnapshot's accept/reject
    // return so out-of-order envelopes leave alerts state untouched.
    const freshSettings = {
      enabled: true,
      weekly_thresholds: [80, 90],
      five_hour_thresholds: [85, 95],
    };
    const freshAlert = alert('weekly:2026-04-27:90');
    // Bootstrap initially returns the fresh snapshot.
    (globalThis as any).fetch = vi.fn().mockResolvedValue({
      json: () =>
        Promise.resolve({
          ...snap('2026-04-24T10:00:00Z'),
          alerts: [freshAlert],
          alerts_settings: freshSettings,
        }),
    });
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    // Apply a NEWER SSE update so subsequent older snapshots are
    // out-of-order. The newer envelope carries an empty alerts list
    // and the same alertsConfig.
    const newerAlert = alert('weekly:2026-04-27:95');
    MockEventSource.instances[0].emit('update', {
      ...snap('2026-04-24T10:01:00Z'),
      alerts: [newerAlert],
      alerts_settings: freshSettings,
    });
    expect(getState().alerts).toEqual([newerAlert]);
    expect(getState().alertsConfig).toEqual(freshSettings);
    const seenBefore = new Set(getState().seenAlertIds);
    expect(seenBefore.has(newerAlert.id)).toBe(true);

    // Now simulate a LATE-arriving bootstrap with an OLDER generated_at,
    // an empty alerts list, and a different (stale) alertsConfig. We
    // synthesize this by emitting an out-of-order SSE update using the
    // same code path (the gate is in updateSnapshot, not the source).
    const staleSettings = {
      enabled: false,
      weekly_thresholds: [50],
      five_hour_thresholds: [],
    };
    const staleAlert = alert('weekly:2026-04-20:60');
    MockEventSource.instances[0].emit('update', {
      ...snap('2026-04-24T09:50:00Z'),  // OLDER than 10:01
      alerts: [staleAlert],
      alerts_settings: staleSettings,
    });
    // updateSnapshot rejected the older envelope; ingestAlerts MUST
    // NOT have run, so alerts / alertsConfig / seenAlertIds are
    // exactly as they were after the post-bootstrap update.
    expect(getState().alerts).toEqual([newerAlert]);
    expect(getState().alertsConfig).toEqual(freshSettings);
    expect(getState().seenAlertIds.has(staleAlert.id)).toBe(false);
  });
});
