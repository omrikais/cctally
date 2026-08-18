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

// #583 S3 §7 — the bootstrap `fetch('/api/data')` is gone. `startSSE` opens the
// EventSource alone and the FIRST accepted update IS the bootstrap, so every
// test that used to seed cold-start state through the fetch stub seeds it
// through the stream instead. The stub stays in place and stays unused, so a
// reintroduced fetch is observable rather than silent.
function seed(env: unknown = snap('2026-04-24T10:00:00Z', 5)): void {
  MockEventSource.instances[0].emit('update', env);
}

beforeEach(() => {
  MockEventSource.instances = [];
  (globalThis as any).EventSource = MockEventSource;
  (globalThis as any).fetch = vi.fn();
  _resetStore();
  _resetSSE();
  localStorage.clear();
});

afterEach(() => {
  closeSSE();
});

describe('startSSE', () => {
  it('bootstraps from the first SSE update with no /api/data fetch', async () => {
    // A cold load used to transfer and parse the envelope TWICE: once through
    // `/api/data` and again as the hub's subscribe seed. The hub publishes
    // before the server binds, so `_last` is never empty when a client
    // connects and the seed alone is a sufficient bootstrap.
    startSSE();
    await Promise.resolve(); await Promise.resolve();
    expect((globalThis as any).fetch).not.toHaveBeenCalled();
    expect(getState().snapshot).toBeNull();
    seed();
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

  it('fires onConnect on the first accepted update, which IS the bootstrap', () => {
    const spy = vi.fn();
    startSSE({ onConnect: spy });
    expect(spy).not.toHaveBeenCalled();
    seed();
    expect(spy).toHaveBeenCalledTimes(1);
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
    seed();
    const bootstrapCalls = spy.mock.calls.length;  // 1 from the first update
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

// #294 S5 Task 7 — the SSE toast pipeline now reads the SOURCE projections
// (`sources.claude/codex.data.alerts.rows`), NOT the legacy top-level `alerts`
// array. Embed the alerts as Claude source rows so `collectToastAlertRows`
// picks them up; the seed-both-forms rule (§6.7) means the bare legacy `id`
// still lands in `seenAlertIds`.
function withSources<T extends object>(env: T, alerts: ReturnType<typeof alert>[]): T {
  return {
    ...env,
    // Real wire shape (#294 S5 QA): the source fields spread at the envelope
    // TOP level; `sources` is the FLAT per-source map. `collectToastAlertRows`
    // reads `env.sources.claude.data.alerts.rows`.
    source_schema_version: 2,
    default_source: 'claude',
    source_order: ['claude', 'codex', 'all'],
    sources: {
      claude: { data: { alerts: { rows: alerts.map((a) => ({ ...a, source: 'claude', key: a.id })) } } },
      codex: { data: { alerts: { rows: [] } } },
    },
  } as T;
}

function snapWithAlerts(generated_at: string, alerts: ReturnType<typeof alert>[]) {
  return withSources({ ...snap(generated_at), alerts }, alerts);
}

describe('startSSE — INGEST_SNAPSHOT_ALERTS wiring (T15)', () => {
  it('cold-start: the FIRST update populates seenAlertIds without surfacing toast', () => {
    startSSE();
    seed(snapWithAlerts('2026-04-24T10:00:00Z', [alert('weekly:2026-04-27:90')]));
    expect(getState().seenAlertIds.has('weekly:2026-04-27:90')).toBe(true);
    expect(getState().toast).toBeNull();
  });

  it('subsequent update with new alert surfaces toast', () => {
    startSSE();
    seed();
    // The first update consumed the cold-start tick; this one is after it.
    MockEventSource.instances[0].emit(
      'update',
      snapWithAlerts('2026-04-24T10:00:05Z', [alert('weekly:2026-04-27:95')]),
    );
    expect(getState().toast?.kind).toBe('alert');
  });

  it('reconnect after onerror re-arms cold-start (next update does not toast)', () => {
    startSSE();
    seed();
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
    seed();
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
      budget_thresholds: [90, 100],
      budget_enabled: true,
    };
    startSSE();
    seed({ ...snap('2026-04-24T10:00:00Z'), alerts: [], alerts_settings: envelopeSettings });
    // #513 S2 §5.1: the seam feeds the store's normalizer, which turns the
    // absent `weekly_usd` leaf into the canonical null.
    expect(getState().alertsConfig).toEqual({ ...envelopeSettings, weekly_usd: null });
  });

  it('normalizes a missing weekly_usd to null when the block IS present (#513 S2 §5.1)', async () => {
    // The compatibility hazard: the seam passes a present `alerts_settings`
    // block through wholesale and defaults only when the WHOLE block is
    // absent, so a server predating the mirror yields `undefined` unless the
    // store normalizes. `undefined` and `null` are distinguishable here.
    startSSE();
    seed({
      ...snap('2026-04-24T10:00:00Z'),
      alerts: [],
      alerts_settings: {
        enabled: false,
        weekly_thresholds: [90, 95],
        five_hour_thresholds: [90, 95],
        budget_thresholds: [],
      },
    });
    expect(getState().alertsConfig.weekly_usd).toBeNull();
    expect('weekly_usd' in getState().alertsConfig).toBe(true);
  });

  it('carries a mirrored weekly_usd amount through the seam (#513 S2 §5.1)', async () => {
    startSSE();
    seed({
      ...snap('2026-04-24T10:00:00Z'),
      alerts: [],
      alerts_settings: {
        enabled: false,
        weekly_thresholds: [90, 95],
        five_hour_thresholds: [90, 95],
        budget_thresholds: [],
        weekly_usd: 250,
      },
    });
    expect(getState().alertsConfig.weekly_usd).toBe(250);
  });

  it('falls back to a null amount when the whole block is absent (#513 S2 §5.1)', async () => {
    startSSE();
    seed({ ...snap('2026-04-24T10:00:00Z'), alerts: [] });
    expect(getState().alertsConfig.weekly_usd).toBeNull();
  });

  it('an out-of-order update is dropped — alerts/alertsConfig/seenAlertIds untouched', () => {
    // Regression: ingestAlerts used to run unconditionally on every envelope,
    // even when updateSnapshot rejected it as out-of-order, so a stale
    // envelope could replace state.alerts and pollute seenAlertIds with stale
    // ids. The fix gates ingestAlerts on updateSnapshot's accept/reject
    // return so out-of-order envelopes leave alerts state untouched.
    // #583 S3 §7: the late BOOTSTRAP that used to arrive out of order is gone
    // with the fetch, but the gate it exposed governs every stream frame and a
    // suspended tab's resume seed can still arrive behind the retained one.
    const freshSettings = {
      enabled: true,
      weekly_thresholds: [80, 90],
      five_hour_thresholds: [85, 95],
      budget_thresholds: [90, 100],
      budget_enabled: true,
    };
    const freshAlert = alert('weekly:2026-04-27:90');
    // The FIRST update carries the fresh snapshot.
    startSSE();
    seed(withSources({
      ...snap('2026-04-24T10:00:00Z'),
      alerts: [freshAlert],
      alerts_settings: freshSettings,
    }, [freshAlert]));
    // Apply a NEWER SSE update so subsequent older snapshots are
    // out-of-order. The newer envelope carries an empty alerts list
    // and the same alertsConfig.
    const newerAlert = alert('weekly:2026-04-27:95');
    MockEventSource.instances[0].emit('update', withSources({
      ...snap('2026-04-24T10:01:00Z'),
      alerts: [newerAlert],
      alerts_settings: freshSettings,
    }, [newerAlert]));
    // #294 S5 — sse now feeds the source pipeline; the observable invariants are
    // alertsConfig + seenAlertIds (state.alerts is no longer sse-populated).
    expect(getState().alertsConfig).toEqual({ ...freshSettings, weekly_usd: null });
    const seenBefore = new Set(getState().seenAlertIds);
    expect(seenBefore.has(newerAlert.id)).toBe(true);

    // Now an OLDER generated_at with a different (stale) alertsConfig.
    const staleSettings = {
      enabled: false,
      weekly_thresholds: [50],
      five_hour_thresholds: [],
      budget_thresholds: [],
      budget_enabled: false,
    };
    const staleAlert = alert('weekly:2026-04-20:60');
    MockEventSource.instances[0].emit('update', withSources({
      ...snap('2026-04-24T09:50:00Z'),  // OLDER than 10:01
      alerts: [staleAlert],
      alerts_settings: staleSettings,
    }, [staleAlert]));
    // updateSnapshot rejected the older envelope; ingestAlerts MUST NOT have
    // run, so alertsConfig / seenAlertIds are exactly as they were after the
    // newer update (the stale alert never got seeded).
    expect(getState().alertsConfig).toEqual({ ...freshSettings, weekly_usd: null });
    expect(getState().seenAlertIds.has(staleAlert.id)).toBe(false);
  });
});
