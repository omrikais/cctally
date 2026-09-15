// #769 S6 / #753 — the client half of the retained hero cohort.
//
// The server publishes the last coherent hero during a reconciliation gap and
// marks it `update_state: "updating"` (D4), or publishes an explicit
// `"pending"` when it has never resolved one (D5). The client used to force
// `spentUsd` to null whenever the provider hero capability read `unavailable`,
// which is exactly what erased a known spend, and `accountScope` reconstructed
// a focused account's spend from the card with a `?? 0` fallback that
// fabricated a zero.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CODEX_HERO_RECONCILING_NOTE, HeroStrip } from './HeroStrip';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.restoreAllMocks();
});

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

function showCodex(env: Envelope) {
  updateSnapshot(env);
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  render(<HeroStrip />);
}

describe('HeroStrip — Codex hero coherence (#753)', () => {
  it('renders the retained figure with a quiet updating marker (D4)', () => {
    // The state the server publishes during the reconciliation gap: real
    // operands, the projection warning, and the capability still unavailable.
    const env = envWith((slice) => {
      const entry = slice.sources!.codex;
      const data = entry.data!;
      data.hero.cost_usd = 61.5;
      data.hero.update_state = 'updating';
      entry.capabilities!.hero = {
        status: 'unavailable', semantics: 'projection-incoherent',
      };
      entry.warnings = [{
        code: 'codex_projection_incoherent',
        message: 'Codex quota projection is unavailable.',
        domain: 'hero',
      }];
    });
    showCodex(env);

    expect(screen.getByText('$61.50')).toBeTruthy();
    expect(screen.getByTestId('hero-spend-updating').textContent).toBe('updating');
    expect(screen.queryByTestId('hero-spend-pending')).toBeNull();
  });

  it('does not blank the figure on the provider capability alone', () => {
    // The narrow statement of the defect: capability `unavailable`, value
    // present, and the figure must survive.
    const env = envWith((slice) => {
      const entry = slice.sources!.codex;
      entry.data!.hero.cost_usd = 12.25;
      entry.capabilities!.hero = {
        status: 'unavailable', semantics: 'projection-incoherent',
      };
    });
    showCodex(env);
    expect(screen.getByText('$12.25')).toBeTruthy();
  });

  it('renders an explicit Pending state, never an em dash (D5)', () => {
    const env = envWith((slice) => {
      const entry = slice.sources!.codex;
      const data = entry.data!;
      data.hero.cost_usd = null;
      data.hero.update_state = 'pending';
      entry.capabilities!.hero = {
        status: 'unavailable', semantics: 'projection-incoherent',
      };
    });
    showCodex(env);

    const pending = screen.getByTestId('hero-spend-pending');
    expect(pending.textContent).toBe('Pending');
    // An em dash beside `SPENT THIS WEEK` reads as "nothing spent", which is
    // the presentation D5 replaces.
    expect(pending.textContent).not.toContain('—');
    expect(screen.queryByTestId('hero-spend-updating')).toBeNull();
  });

  it('shows neither marker on a coherent generation', () => {
    const env = envWith((slice) => {
      slice.sources!.codex.data!.hero.cost_usd = 40;
    });
    showCodex(env);
    expect(screen.queryByTestId('hero-spend-updating')).toBeNull();
    expect(screen.queryByTestId('hero-spend-pending')).toBeNull();
  });

  // #769 S6 / #753 browser QA P2 — the reconciling state explained itself only
  // in a `title` on a non-interactive element, and the containing `.hero-spent`
  // zone returned null for `aria-label`. A `title` is hover-only, so a touch
  // user had no way to learn why the figure was retained. This repository
  // already recorded and fixed this class for this same zone (public #5 QA P2):
  // the reason belongs in the zone's accessible name AND in a visible line.
  it('states why the figure is retained without needing hover', () => {
    const env = envWith((slice) => {
      const data = slice.sources!.codex.data!;
      data.hero.cost_usd = 61.5;
      data.hero.update_state = 'updating';
    });
    showCodex(env);

    const zone = document.querySelector('.hero-spent')!;
    expect(zone.getAttribute('aria-label')).toContain(CODEX_HERO_RECONCILING_NOTE);
    // A visible line as well: an accessible name is not reachable by a sighted
    // touch user, and the marker alone reads as a bare word.
    const visible = document.querySelector('[data-testid="hero-spent-note"]')!;
    expect(visible.textContent).toContain('last coherent figure');
  });

  it('leaves the zone label alone on a coherent generation', () => {
    const env = envWith((slice) => {
      slice.sources!.codex.data!.hero.cost_usd = 40;
    });
    showCodex(env);
    const zone = document.querySelector('.hero-spent')!;
    expect(zone.getAttribute('aria-label') ?? '').not.toContain(CODEX_HERO_RECONCILING_NOTE);
  });

  it('uses the design system chip vocabulary for the updating marker', () => {
    // `chip chip-aging` is the muted slate disclosure variant. Deliberately not
    // `chip-stale`, which is the amber alarm treatment: the figure beside this
    // marker is correct.
    const env = envWith((slice) => {
      const data = slice.sources!.codex.data!;
      data.hero.cost_usd = 5;
      data.hero.update_state = 'updating';
    });
    showCodex(env);
    const marker = screen.getByTestId('hero-spend-updating');
    expect(marker.className.split(/\s+/)).toContain('chip');
    expect(marker.className.split(/\s+/)).toContain('chip-aging');
    expect(marker.className.split(/\s+/)).not.toContain('chip-stale');
  });
});
