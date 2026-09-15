// #769 S6 / #753 browser QA P2 — the hero and the current-usage modal describe
// one projection gap in opposite words.
//
// During the gap the server publishes the last coherent hero with
// `update_state: "updating"` and repeats the projection warning. The hero shows
// the retained figure beside a quiet marker whose sentence says the projection
// is reconciling and the figure is the last coherent one. The modal printed the
// warning verbatim — "Codex quota projection is unavailable." — directly above
// the same published SPENT figure, the $/1% value, the percentage and the reset
// time. A reader of both surfaces cannot tell whether the figure is real.
//
// The hero's framing is the correct one, so the modal moves toward the hero.
// The Codex-only view, which consulted no qualification at all because
// `providerReason` was read only on the combined All path, gains the same
// sentence.
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import { CODEX_HERO_RECONCILING_NOTE } from '../components/HeroStrip';
import type { DashboardSelection, Envelope } from '../types/envelope';
import { CurrentWeekModal } from './CurrentWeekModal';

const PROJECTION_WARNING = 'Codex quota projection is unavailable.';

function envWith(mut?: (b: ReturnType<typeof makeSourceEnvelope>) => void): Envelope {
  const slice = makeSourceEnvelope();
  mut?.(slice);
  return {
    header: {
      used_pct: 17.4, week_label: 'wk', five_hour_pct: null,
      dollar_per_pct: 1.2, forecast_pct: 60, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    ...slice,
  } as unknown as Envelope;
}

// The reconciliation gap exactly as the server publishes it: a real retained
// figure, the marker, the warning, and the capability still unavailable.
function reconcilingEnv(): Envelope {
  return envWith((b) => {
    const entry = b.sources.codex;
    (entry.data!.hero as unknown as { update_state?: string }).update_state = 'updating';
    entry.capabilities!.hero = { status: 'unavailable', semantics: 'projection-incoherent' };
    entry.warnings = [{
      code: 'codex_projection_incoherent',
      message: PROJECTION_WARNING,
      domain: 'hero',
    }];
  });
}

function renderFor(source: DashboardSelection, env: Envelope) {
  act(() => {
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
  });
  return render(<CurrentWeekModal />);
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});
afterEach(() => cleanup());

describe('CurrentWeekModal — projection gap wording (#753 QA P2)', () => {
  it('says what the hero says on the All path, not that the projection is unavailable', () => {
    const { container } = renderFor('all', reconcilingEnv());
    const section = container.querySelector('[data-provider-section="codex"]')!;
    expect(section.textContent).toContain(CODEX_HERO_RECONCILING_NOTE);
    expect(section.textContent).not.toContain(PROJECTION_WARNING);
    // The figures beneath are published and correct, and stay published.
    expect(section.textContent).toContain('$12.30');
  });

  it('carries the qualification in the Codex-only view, which dropped it entirely', () => {
    const { container } = renderFor('codex', reconcilingEnv());
    const note = container.querySelector('[data-testid="codex-hero-reconciling-note"]');
    expect(note).not.toBeNull();
    expect(note!.textContent).toBe(CODEX_HERO_RECONCILING_NOTE);
    expect(container.querySelector('.mcw-mini')!.textContent).toContain('$12.30');
  });

  it('does not repeat the note inside the embedded All variant', () => {
    // The embedded Codex section already prints the reason above it, exactly as
    // the stale-cycle note is suppressed there (spec §3.7).
    const { container } = renderFor('all', reconcilingEnv());
    expect(
      container.querySelector('[data-testid="codex-hero-reconciling-note"]'),
    ).toBeNull();
  });

  it('leaves an ordinary Codex modal untouched', () => {
    const { container } = renderFor('codex', envWith());
    expect(
      container.querySelector('[data-testid="codex-hero-reconciling-note"]'),
    ).toBeNull();
  });
});
