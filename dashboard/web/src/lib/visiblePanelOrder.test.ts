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

describe('deriveVisiblePanelOrder — hidden panels excluded', () => {
  it('Claude shows every grid panel (cache_report present)', () => {
    expect(deriveVisiblePanelOrder(FULL, viewFor('claude'))).toEqual(FULL);
  });

  it('Codex excludes trend / cache-report / forecast', () => {
    expect(deriveVisiblePanelOrder(FULL, viewFor('codex'))).toEqual([
      'sessions',
      'projects',
      'daily',
      'weekly',
      'monthly',
      'blocks',
      'alerts',
    ]);
  });

  it('All shows every grid panel (Claude-only forecast/trend still render as sections)', () => {
    expect(deriveVisiblePanelOrder(FULL, viewFor('all'))).toEqual(FULL);
  });

  it('Codex wholly-unavailable STILL excludes trend / cache-report / forecast (digit renumbering)', () => {
    // The QA P0 environment: Codex availability 'unavailable', caps {}, data null.
    // Absent-path panels must hide regardless of availability; the rest degrade
    // (visible), so the visible order matches the healthy-Codex order.
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
    expect(deriveVisiblePanelOrder(FULL, resolveSourceView(env, 'codex'))).toEqual([
      'sessions',
      'projects',
      'daily',
      'weekly',
      'monthly',
      'blocks',
      'alerts',
    ]);
  });

  it('never mutates the persisted full order (a switch returns a new array)', () => {
    const before = [...FULL];
    deriveVisiblePanelOrder(FULL, viewFor('codex'));
    deriveVisiblePanelOrder(FULL, viewFor('claude'));
    expect(FULL).toEqual(before);
  });
});

describe('deriveVisiblePanelOrder — digit-position semantics', () => {
  it('digit N addresses the Nth VISIBLE panel', () => {
    const visible = deriveVisiblePanelOrder(FULL, viewFor('codex'));
    // Codex: `2` opens the 2nd visible panel = projects (trend is hidden).
    expect(visible[1]).toBe('projects');
    // `5` opens the 5th visible = monthly.
    expect(visible[4]).toBe('monthly');
  });
});

describe('mapVisibleReorderToFull — round-trips a reorder, holds hidden panels', () => {
  const codexVisible = deriveVisiblePanelOrder(FULL, viewFor('codex'));

  it('an identity reorder is the identity on the full order', () => {
    expect(mapVisibleReorderToFull(FULL, codexVisible, codexVisible)).toEqual(FULL);
  });

  it('moves visible panels among their slots while hidden panels keep their indices', () => {
    // Move 'alerts' (last visible) to the front of the visible list.
    const after: GridPanelId[] = ['alerts', ...codexVisible.filter((p) => p !== 'alerts')];
    const full2 = mapVisibleReorderToFull(FULL, codexVisible, after);
    // Hidden panels stay at their exact original indices.
    expect(full2[FULL.indexOf('trend')]).toBe('trend');
    expect(full2[FULL.indexOf('cache-report')]).toBe('cache-report');
    expect(full2[FULL.indexOf('forecast')]).toBe('forecast');
    // The visible subsequence of the new full order equals the reordered list.
    expect(deriveVisiblePanelOrder(full2, viewFor('codex'))).toEqual(after);
    // Same length, same members — nothing lost.
    expect([...full2].sort()).toEqual([...FULL].sort());
  });
});
