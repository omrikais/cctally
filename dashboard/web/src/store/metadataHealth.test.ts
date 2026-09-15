// #834 S2 (#829) — one normalizer for both client transports.
//
// The case that exists is a NEW client reading an OLDER server's payload: the
// dashboard `execvp`s itself on an in-place update while an already-loaded
// client reconnects over its existing EventSource, so the bundle in the page
// can be newer than the payload it is handed. In that payload
// `metadata_health` is absent, and absence must normalize to UNKNOWN. Reading
// it as healthy would render a clean bill of health nobody issued.
import { describe, expect, it, vi } from 'vitest';
import {
  METADATA_HEALTH_UNKNOWN,
  normalizeEnvelopeMetadataHealth,
  normalizeMetadataHealth,
} from '../types/envelope';
import { DashboardStreamHub } from './dashboardStreamHub';
import { createDashboardStreamTransport } from './dashboardStreamTransport';

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

class FakePort {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onmessageerror: (() => void) | null = null;
  sent: unknown[] = [];
  closed = false;
  postMessage(value: unknown) { this.sent.push(value); }
  start() {}
  close() { this.closed = true; }
  clientMessage(value: unknown) { this.onmessage?.({ data: value } as MessageEvent); }
}

function sourceEntry(extra: Record<string, unknown> = {}) {
  return {
    availability: 'ok',
    freshness: 'fresh',
    warnings: [],
    data_version: 'v1',
    last_success_at: null,
    capabilities: {},
    data: null,
    ...extra,
  };
}

function envelope(sources: Record<string, unknown>, schema: number | undefined) {
  return {
    envelope_version: 2,
    generated_at: '2026-09-13T00:00:00Z',
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
    ...(schema == null ? {} : { source_schema_version: schema }),
    sources,
  };
}

/** A pre-v12 server: every source entry, and no `metadata_health` anywhere. */
const OLD_SERVER_PAYLOAD = envelope(
  { claude: sourceEntry(), codex: sourceEntry(), all: sourceEntry() },
  11,
);

const NEW_SERVER_PAYLOAD = envelope(
  {
    claude: sourceEntry({ metadata_health: null }),
    codex: sourceEntry({
      metadata_health: {
        state: 'malformed_row_partial', incomplete_rows: 4, retryable: false,
      },
    }),
    all: sourceEntry({ metadata_health: null }),
  },
  12,
);

describe('normalizeMetadataHealth', () => {
  it('normalizes an absent object to unknown, never to healthy', () => {
    expect(normalizeMetadataHealth(undefined)).toEqual(METADATA_HEALTH_UNKNOWN);
    expect(normalizeMetadataHealth(undefined).state).toBe('unknown');
  });

  it('normalizes an explicit null to unknown too', () => {
    // A v12 server publishes null on a provider that describes no Codex
    // metadata. The two are different facts and the same rendering: neither
    // is a statement that attribution is complete.
    expect(normalizeMetadataHealth(null)).toEqual(METADATA_HEALTH_UNKNOWN);
  });

  it('preserves each of the three server states verbatim', () => {
    expect(normalizeMetadataHealth({
      state: 'healthy', incomplete_rows: 0, retryable: false,
    })).toEqual({ state: 'healthy', incomplete_rows: 0, retryable: false });
    expect(normalizeMetadataHealth({
      state: 'malformed_row_partial', incomplete_rows: 7, retryable: false,
    })).toEqual({
      state: 'malformed_row_partial', incomplete_rows: 7, retryable: false,
    });
    expect(normalizeMetadataHealth({
      state: 'transient_read_failure', incomplete_rows: null, retryable: true,
    })).toEqual({
      state: 'transient_read_failure', incomplete_rows: null, retryable: true,
    });
  });

  it('rejects a state it does not know rather than passing it through', () => {
    // A future server could add a fourth state. This bundle cannot render it,
    // and rendering it as healthy would be the one unrecoverable mistake.
    expect(normalizeMetadataHealth({
      state: 'something_new', incomplete_rows: 3, retryable: false,
    })).toEqual(METADATA_HEALTH_UNKNOWN);
    expect(normalizeMetadataHealth('healthy')).toEqual(METADATA_HEALTH_UNKNOWN);
    expect(normalizeMetadataHealth({ state: 'healthy' })).toEqual({
      state: 'healthy', incomplete_rows: null, retryable: false,
    });
  });

  it('fills every source entry of an older payload', () => {
    const normalized = normalizeEnvelopeMetadataHealth(
      JSON.parse(JSON.stringify(OLD_SERVER_PAYLOAD)),
    );
    for (const name of ['claude', 'codex', 'all'] as const) {
      expect(normalized.sources?.[name]?.metadata_health)
        .toEqual(METADATA_HEALTH_UNKNOWN);
    }
  });

  it('leaves a v12 payload saying exactly what the server said', () => {
    const normalized = normalizeEnvelopeMetadataHealth(
      JSON.parse(JSON.stringify(NEW_SERVER_PAYLOAD)),
    );
    expect(normalized.sources?.codex?.metadata_health).toEqual({
      state: 'malformed_row_partial', incomplete_rows: 4, retryable: false,
    });
    expect(normalized.sources?.claude?.metadata_health)
      .toEqual(METADATA_HEALTH_UNKNOWN);
  });

  it('tolerates an envelope with no sources map at all', () => {
    const bare = envelope({}, undefined) as Record<string, unknown>;
    delete bare.sources;
    expect(() => normalizeEnvelopeMetadataHealth(bare as never)).not.toThrow();
  });
});

describe('both transports normalize through the same function', () => {
  it('the direct EventSource transport normalizes an older payload', () => {
    const sources: FakeEventSource[] = [];
    const original = globalThis.EventSource;
    (globalThis as Record<string, unknown>).EventSource = function () {
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    } as never;
    const onSnapshot = vi.fn((_snapshot: unknown) => true);
    try {
      createDashboardStreamTransport(
        { onReady: () => {}, onSnapshot, onError: () => {} },
      );
      sources[0].update(JSON.stringify(OLD_SERVER_PAYLOAD));
    } finally {
      (globalThis as Record<string, unknown>).EventSource = original as never;
    }
    expect(onSnapshot).toHaveBeenCalledTimes(1);
    const snapshot = onSnapshot.mock.calls[0]![0] as
      { sources: Record<string, { metadata_health: unknown }> };
    expect(snapshot.sources.codex.metadata_health)
      .toEqual(METADATA_HEALTH_UNKNOWN);
  });

  it('the shared-worker hub normalizes the same payload identically', () => {
    const sources: FakeEventSource[] = [];
    const hub = new DashboardStreamHub(() => {
      const source = new FakeEventSource();
      sources.push(source);
      return source;
    });
    const port = new FakePort();
    hub.connect(port as never);
    port.clientMessage({ version: 1, type: 'subscribe', generation: 1 });
    sources[0].update(JSON.stringify(OLD_SERVER_PAYLOAD));

    const delivered = port.sent.at(-1) as {
      snapshot: { sources: Record<string, { metadata_health: unknown }> };
    };
    expect(delivered.snapshot.sources.codex.metadata_health)
      .toEqual(METADATA_HEALTH_UNKNOWN);
  });
});

describe('an older client meets a v12 payload', () => {
  it('reads the preserved legacy detail fields unchanged', () => {
    // The additive half of the same transition. A pre-#829 bundle knows
    // nothing about `metadata_health` and reads `metadata_availability` /
    // `metadata_reason` on the detail body. Those keys are PRESERVED; the
    // only change is that a healthy detail now carries them explicitly null
    // rather than omitting them, and `d.metadata_availability === 'partial'`
    // — the exact predicate every render site uses — is false either way.
    const healthy = { metadata_availability: null, metadata_reason: null };
    const omitted: Record<string, unknown> = {};
    const partial = {
      metadata_availability: 'partial',
      metadata_reason: 'Project metadata is unavailable for this item.',
    };
    expect(healthy.metadata_availability === 'partial').toBe(false);
    expect(omitted.metadata_availability === 'partial').toBe(false);
    expect(partial.metadata_availability === 'partial').toBe(true);
  });
});
