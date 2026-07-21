import { describe, expect, it } from 'vitest';
import { deriveVisiblePanelOrder, mapVisibleReorderToFull } from './visiblePanelOrder';
import { DEFAULT_PANEL_ORDER, type GridPanelId } from './panelIds';
import { resolveSourceView, type SourceView } from '../store/sourceView';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { DashboardSelection, Envelope } from '../types/envelope';

function viewFor(selection: DashboardSelection): SourceView {
  const env = {
    ...makeSourceEnvelope(),
    cache_report: { is_empty: false },
  } as unknown as Envelope;
  return resolveSourceView(env, selection);
}

const FULL: GridPanelId[] = [...DEFAULT_PANEL_ORDER];

describe('deriveVisiblePanelOrder — canonical board parity', () => {
  it('Claude shows every grid panel (cache_report present)', () => {
    expect(deriveVisiblePanelOrder(FULL, viewFor('claude'))).toEqual(FULL);
  });

  it('Codex preserves all ten canonical cards', () => {
    expect(deriveVisiblePanelOrder(FULL, viewFor('codex'))).toEqual(FULL);
  });

  it('All shows every grid panel (Claude-only forecast/trend still render as sections)', () => {
    expect(deriveVisiblePanelOrder(FULL, viewFor('all'))).toEqual(FULL);
  });

  it('Codex wholly-unavailable still preserves card shells and digit positions', () => {
    // The QA P0 environment: Codex availability 'unavailable', caps {}, data
    // null. Provider state changes content inside the fixed shells, never the
    // canonical board membership.
    const codex = makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      capabilities: {},
      warnings: [{ code: 'source_build_failed', message: 'Source data could not be built.' }],
      last_success_at: null,
    });
    const claude = makeClaudeSourceEntry();
    const env = {
      ...makeSourceEnvelope({ sources: { claude, codex, all: makeAllSourceEntry(claude, codex) } }),
      cache_report: { is_empty: false },
    } as unknown as Envelope;
    expect(deriveVisiblePanelOrder(FULL, resolveSourceView(env, 'codex'))).toEqual(FULL);
  });

  it('never mutates the persisted full order (a switch returns a new array)', () => {
    const before = [...FULL];
    deriveVisiblePanelOrder(FULL, viewFor('codex'));
    deriveVisiblePanelOrder(FULL, viewFor('claude'));
    expect(FULL).toEqual(before);
  });
});

describe('deriveVisiblePanelOrder — digit-position semantics', () => {
  it('digit N addresses the same canonical position for every source', () => {
    const visible = deriveVisiblePanelOrder(FULL, viewFor('codex'));
    expect(visible[1]).toBe('trend');
    expect(visible[4]).toBe('cache-report');
  });
});

describe('mapVisibleReorderToFull — round-trips a canonical-board reorder', () => {
  const codexVisible = deriveVisiblePanelOrder(FULL, viewFor('codex'));

  it('an identity reorder is the identity on the full order', () => {
    expect(mapVisibleReorderToFull(FULL, codexVisible, codexVisible)).toEqual(FULL);
  });

  it('moves all canonical panels without losing members', () => {
    // Move 'alerts' (last visible) to the front of the visible list.
    const after: GridPanelId[] = ['alerts', ...codexVisible.filter((p) => p !== 'alerts')];
    const full2 = mapVisibleReorderToFull(FULL, codexVisible, after);
    expect(full2[0]).toBe('alerts');
    expect(deriveVisiblePanelOrder(full2, viewFor('codex'))).toEqual(after);
    // Same length, same members — nothing lost.
    expect([...full2].sort()).toEqual([...FULL].sort());
  });
});
