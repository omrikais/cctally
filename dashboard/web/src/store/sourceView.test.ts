import { describe, expect, it } from 'vitest';
import {
  CLAUDE_LEGACY_FALLBACK_FIELDS,
  isHydratingEntry,
  resolveSourceView,
} from './sourceView';
import {
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeHydratingEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

function envWithSources(): Envelope {
  return makeSourceEnvelope() as unknown as Envelope;
}

describe('resolveSourceView — per-selection resolution', () => {
  it('resolves each selection to its own entry from the flat sources map', () => {
    const env = envWithSources();
    for (const sel of ['claude', 'codex', 'all'] as const) {
      const view = resolveSourceView(env, sel);
      expect(view.selection).toBe(sel);
      expect(view.entry).toBe(env.sources![sel]);
      expect(view.hydrating).toBe(false);
      expect(view.env).toBe(env);
    }
  });
});

describe('resolveSourceView — hydration detection (§5.2)', () => {
  it('env == null is hydrating for every selection with a null entry', () => {
    for (const sel of ['claude', 'codex', 'all'] as const) {
      const view = resolveSourceView(null, sel);
      expect(view.entry).toBeNull();
      expect(view.hydrating).toBe(true);
      expect(view.env).toBeNull();
    }
  });

  it('an entry with the exact §5.2 hydrating shape is hydrating', () => {
    const slice = makeSourceEnvelope();
    slice.sources.codex = makeHydratingEntry() as unknown as typeof slice.sources.codex;
    const env = slice as unknown as Envelope;
    const view = resolveSourceView(env, 'codex');
    expect(view.hydrating).toBe(true);
  });

  it('isHydratingEntry keys off the shape, not availability', () => {
    // The server publishes the hydrating seed as availability:'partial'.
    expect(isHydratingEntry(makeHydratingEntry())).toBe(true);
    // A real ok entry is not hydrating.
    expect(isHydratingEntry(makeCodexSourceEntry())).toBe(false);
    // A degraded entry (has data + a warning) is not hydrating.
    const degraded = makeCodexSourceEntry({
      availability: 'partial',
      freshness: 'stale',
      warnings: [{ code: 'x', message: 'stale' }],
    });
    expect(isHydratingEntry(degraded)).toBe(false);
    // An unavailable entry (has a warning) is not hydrating.
    const unavailable = makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      capabilities: {},
      warnings: [{ code: 'y', message: 'gone' }],
      last_success_at: null,
    });
    expect(isHydratingEntry(unavailable)).toBe(false);
    expect(isHydratingEntry(null)).toBe(false);
  });
});

describe('resolveSourceView — legacy fallback constant', () => {
  it('CLAUDE_LEGACY_FALLBACK_FIELDS has exactly alerts_settings + cache_report', () => {
    expect([...CLAUDE_LEGACY_FALLBACK_FIELDS]).toEqual(['alerts_settings', 'cache_report']);
  });
});

describe('resolveSourceView — pre-S4 envelope (sources absent)', () => {
  it('resolves Claude to a legacy-compatible view (entry null, not hydrating)', () => {
    const env = { header: {} } as unknown as Envelope; // no `sources`
    const view = resolveSourceView(env, 'claude');
    expect(view.entry).toBeNull();
    expect(view.hydrating).toBe(false);
    expect(view.env).toBe(env);
  });

  it('resolves Codex/All to hydrating-like absence without crashing', () => {
    const env = { header: {} } as unknown as Envelope;
    for (const sel of ['codex', 'all'] as const) {
      const view = resolveSourceView(env, sel);
      expect(view.entry).toBeNull();
      expect(view.hydrating).toBe(true);
    }
  });
});

// Ensure the Claude builder resolves distinctly too (non-vacuity anchor).
describe('resolveSourceView — Claude with sources present', () => {
  it('resolves the Claude entry (not hydrating) and keeps env', () => {
    const slice = makeSourceEnvelope();
    slice.sources.claude = makeClaudeSourceEntry();
    const env = slice as unknown as Envelope;
    const view = resolveSourceView(env, 'claude');
    expect(view.hydrating).toBe(false);
    expect(view.entry).toBe(slice.sources.claude);
  });
});
