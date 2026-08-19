import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { HeroStrip } from './HeroStrip';
import { SourceStatusChip } from './SourceStatusChip';
import { resolveSourceView } from '../store/sourceView';
import { gateSessions } from '../lib/sourceGating';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import {
  ACCOUNT_B,
  makeAllSourceEntry,
  makeDecoratedCodexSourceData,
  makeHydratingEntry,
  makeSourceEnvelope,
  withSharedRootWeeklyWindows,
} from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.restoreAllMocks();
});

function canonicalStructure(el: Element): unknown {
  return {
    tag: el.tagName,
    className: el.getAttribute('class'),
    metric: el.getAttribute('data-metric'),
    children: Array.from(el.children, canonicalStructure),
  };
}

function parityEnv(): Envelope {
  const env = envWith();
  env.header.vs_last_week_delta = (12.3 / 61) - 0.25;
  env.current_week = {
    used_pct: 17.4,
    five_hour_pct: 9,
    five_hour_resets_in_sec: null,
    spent_usd: 8.4,
    dollar_per_pct: 0.48,
    reset_at_utc: '2026-04-30T00:00:00Z',
    reset_in_sec: 216000,
    last_snapshot_age_sec: 420,
    milestones: [],
    freshness: {
      label: 'fresh',
      captured_at: '2026-04-24T13:00:00Z',
      age_seconds: 420,
    },
    five_hour_block: null,
  };
  const codex = env.sources!.codex.data!;
  const current = codex.periods.weekly.rows[0];
  codex.periods.weekly.rows = [
    {
      ...current,
      label: '04-23 00:00',
      cost_usd: 12.3,
      start_at: '2026-04-23T00:00:00Z',
      end_at: '2026-04-30T00:00:00Z',
      used_pct: 61,
      dollar_per_pct: 12.3 / 61,
    },
    {
      ...current,
      label: '04-16 00:00',
      cost_usd: 16.25,
      start_at: '2026-04-16T00:00:00Z',
      end_at: '2026-04-23T00:00:00Z',
      used_pct: 65,
      dollar_per_pct: 0.25,
    },
  ];
  return env;
}

function envWith(mut?: (b: ReturnType<typeof makeSourceEnvelope>) => void): Envelope {
  const slice = makeSourceEnvelope();
  mut?.(slice);
  return {
    header: { used_pct: 17.4, week_label: 'wk', five_hour_pct: null, dollar_per_pct: 1.2, forecast_pct: 60, forecast_verdict: 'ok', vs_last_week_delta: null },
    current_week: null,
    ...slice,
  } as unknown as Envelope;
}

describe('HeroStrip — Codex tiles (§6.1)', () => {
  beforeEach(() => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  it('does not evaluate an account-blind parent forecast under decorated All', () => {
    const slice = makeSourceEnvelope();
    const data = withSharedRootWeeklyWindows(makeDecoratedCodexSourceData());
    for (const row of data.quota.histories.filter(
      (history) => history.window_minutes === 10_080,
    )) {
      Object.defineProperty(row, 'forecast', {
        get() {
          throw new Error(
            `account-blind parent forecast read for ${row.account_key ?? ACCOUNT_B}`,
          );
        },
      });
    }
    const codex = { ...slice.sources.codex, data };
    updateSnapshot({
      header: {
        used_pct: 17.4,
        week_label: 'wk',
        five_hour_pct: null,
        dollar_per_pct: 1.2,
        forecast_pct: 60,
        forecast_verdict: 'ok',
        vs_last_week_delta: null,
      },
      current_week: null,
      ...slice,
      sources: {
        ...slice.sources,
        codex,
        all: makeAllSourceEntry(slice.sources.claude, codex),
      },
    } as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });

    expect(() => render(<HeroStrip />)).not.toThrow();
    expect(screen.getByTestId('hero-leg-codex')).toHaveTextContent('$10.15 · 3 accounts');
  });

  it('uses Claude\'s exact hero structure and metric slots with Codex cycle data', () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-04-24T13:07:00Z'));
    const env = parityEnv();
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<HeroStrip />);
    const claudeStructure = canonicalStructure(container.querySelector('.hero-strip')!);

    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    const hero = container.querySelector('.hero-strip')!;
    expect(canonicalStructure(hero)).toEqual(claudeStructure);
    expect(hero).toHaveTextContent('WEEK USAGE · Apr 23–Apr 30');
    expect(hero).toHaveTextContent('61.0%');
    expect(hero).toHaveTextContent('5-HOUR42%');
    expect(hero).toHaveTextContent('SPENT THIS WEEK$12');
    expect(hero).toHaveTextContent('$0.20 / 1% used');
    expect(hero).toHaveTextContent('Forecast @ reset80%');
    expect(hero).toHaveTextContent('$/1% vs last week$0.05');
    expect(hero).toHaveTextContent('Snapshot7m ago');
    expect(hero).not.toHaveTextContent('total tokens');
    expect(hero).not.toHaveTextContent('Budget');
  });

  it('opens the source-aware current-cycle modal instead of a status toast', () => {
    const { container } = render(<HeroStrip />);
    const hero = container.querySelector('.hero-strip') as HTMLElement;
    act(() => { hero.click(); });
    expect(getState().openModal).toBe('current-week');
    expect(getState().toast).toBeNull();
  });

  it('makes an unavailable native reset cycle explicit without rendering zero spend', () => {
    updateSnapshot(
      envWith((b) => {
        const codex = b.sources.codex;
        b.sources.codex = {
          ...codex,
          availability: 'partial',
          freshness: 'fresh',
          warnings: [{
            code: 'codex_cycle_unavailable',
            message: 'Codex native reset cycle is unavailable.',
            domain: 'hero',
          }],
          capabilities: {
            ...codex.capabilities,
            hero: {
              status: 'unavailable',
              semantics: 'missing-or-conflicting-native-cycle',
            },
          },
          data: {
            ...codex.data!,
            hero: {
              ...codex.data!.hero,
              cost_usd: null,
              input_tokens: null,
              cached_input_tokens: null,
              output_tokens: null,
              reasoning_output_tokens: null,
              total_tokens: null,
              cycle: null,
            },
          } as unknown as typeof codex.data,
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<HeroStrip />);
    const unavailable = document.querySelector('.hero-spent')!;
    expect(unavailable).toHaveTextContent('—');
    expect(unavailable).toHaveAttribute('title', 'Codex native reset cycle is unavailable.');
    expect(document.querySelector('.hero-usage')).toHaveTextContent('5-HOUR');
    expect(document.querySelector('.hero-support')).toHaveTextContent('$/1% vs last week');
  });

  it('uses the canonical week and $/1% vocabulary under Codex', () => {
    const { container } = render(<HeroStrip />);
    expect(container.textContent).toContain('/ 1% used');
    expect(container.textContent).toContain('WEEK USAGE');
    expect(container.textContent).toContain('SPENT THIS WEEK');
  });

  it('treats a missing 300-minute limit as healthy and keeps weekly cycle spend', () => {
    updateSnapshot(envWith((b) => {
      const data = b.sources.codex.data!;
      data.quota.histories = data.quota.histories.filter((row) => row.window_minutes !== 300);
      data.quota.blocks = data.quota.blocks.filter((row) => !row.label.includes('5-hour'));
      data.hero.quota.active = data.hero.quota.active.filter((row) => row.key !== 'quota:codex-5h');
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<HeroStrip />);
    expect(document.querySelector('.hero-usage')).not.toHaveTextContent('5-HOUR');
    expect(document.querySelector('.hero-spent')).toHaveTextContent('$12');
    expect(screen.queryByText(/unavailable/i)).not.toBeInTheDocument();
  });

  it('displays a restored 300-minute limit independently without changing cycle spend', () => {
    render(<HeroStrip />);
    expect(document.querySelector('.hero-usage')).toHaveTextContent('5-HOUR42%');
    expect(document.querySelector('.hero-spent')).toHaveTextContent('$12');
  });
});

// =========================================================================
// #350 — a stale-but-valid Codex cycle keeps its ACTUALS and blanks only the
// projection. Spec §3.0: "Projections blank. Actuals stay."
// =========================================================================

function staleCycleEnv(): Envelope {
  const env = parityEnv();
  const codex = env.sources!.codex.data!;
  // Build time stamped the additive hero-local marker; the envelope metadata
  // (availability / freshness / warnings / capabilities) is deliberately
  // untouched, because five separate gates read it as one meaning.
  (codex.hero as unknown as { cycle_freshness?: string }).cycle_freshness = 'stale';
  const weekly = codex.quota.histories.find((row) => row.window_minutes === 10_080)!;
  weekly.freshness = 'stale';
  weekly.forecast.status = 'stale';
  // The clock PRESERVES projected_percent alongside a stale status — the hero
  // must gate on the status, not on the value being null.
  weekly.forecast.projected_percent = 80.0;
  const active = codex.hero.quota.active.find((row) => row.key === weekly.key)!;
  active.freshness = 'stale';
  // Two hours old, so the client-derived Snapshot chip reads `stale`.
  active.captured_at = '2026-04-24T11:00:00Z';
  return env;
}

describe('HeroStrip — stale Codex cycle disclosure (#350)', () => {
  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-04-24T13:07:00Z'));
    updateSnapshot(staleCycleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  it('keeps every backward-looking actual and blanks only the forecast', () => {
    const { container } = render(<HeroStrip />);
    const hero = container.querySelector('.hero-strip')!;

    expect(hero).toHaveTextContent('SPENT THIS WEEK$12');
    expect(hero).toHaveTextContent('$0.20 / 1% used');
    expect(hero).toHaveTextContent('$/1% vs last week$0.05');
    expect(hero).toHaveTextContent('WEEK USAGE · Apr 23–Apr 30');
    expect(hero).toHaveTextContent('61.0%');
    // The projection pauses through its OWN forecast.status gate.
    expect(hero).toHaveTextContent('Forecast @ reset—');
    // Token totals are retained on the wire but never displayed for Codex.
    expect(hero).not.toHaveTextContent('total tokens');
  });

  it('reads the existing Snapshot stale marker rather than a new visual', () => {
    render(<HeroStrip />);
    const chip = document.querySelector('.sup-fresh')!;

    expect(chip).toHaveAttribute('data-freshness', 'stale');
    expect(chip).toHaveTextContent('⚠');
    // The state must be NAMED, not only tinted: colour plus a glyph is
    // unreadable to a colourblind user and carries no meaning on its own.
    expect(chip).toHaveTextContent(/stale/i);
    expect(chip.getAttribute('title')).toMatch(/stale/i);
  });

  it('discloses the stale cycle on the hero-spent zone without hiding the spend', () => {
    render(<HeroStrip />);
    const spent = document.querySelector('.hero-spent')!;

    expect(spent).toHaveTextContent('$12');
    expect(spent.getAttribute('title')).toMatch(/stale/i);
    // `title` alone is hover-only — unreachable on touch and unreliable for
    // screen readers, so the reason must also ride an aria-label.
    expect(spent.getAttribute('aria-label')).toMatch(/stale/i);
  });

  it('leaves the source status chip reading fresh, never "Hero unavailable"', () => {
    render(<SourceStatusChip />);
    const chip = screen.getByTestId('source-status-chip');

    expect(chip).toHaveTextContent('fresh');
    expect(chip.textContent).not.toContain('Hero unavailable');
    expect(chip.className).not.toContain('is-degraded');
  });

  it('leaves Sessions gating untouched', () => {
    const view = resolveSourceView(staleCycleEnv(), 'codex');
    expect(gateSessions(view).mode).toBe('render');
  });
});

describe('HeroStrip — All combined tiles (§6.1)', () => {
  it('opens the provider-composed Current Week modal', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<HeroStrip />);

    act(() => { (container.querySelector('.hero-strip') as HTMLElement).click(); });

    expect(getState().openModal).toBe('current-week');
    expect(getState().openModalSource).toBe('all');
    expect(getState().toast).toBeNull();
  });

  it('shows the combined {cost_usd, total_tokens} when non-null', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const combined = screen.getByTestId('shared-hero-spent');
    // claude 8.4 + codex 12.3 = 20.7
    expect(combined).toHaveTextContent('$20.70');
    expect(combined).not.toHaveTextContent('unavailable');
  });

  it('keeps certified spend visible while omitting an uncertified token total', () => {
    updateSnapshot(envWith((b) => {
      const combined = b.sources.all.data!.combined!;
      combined.total_tokens = null;
      combined.legs.claude.total_tokens = null;
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    const combined = screen.getByTestId('shared-hero-spent');
    expect(combined).toHaveTextContent('$20.70');
    expect(combined).not.toHaveTextContent('total tokens');
    expect(combined).not.toHaveTextContent('0 total tokens');
    expect(combined).not.toHaveTextContent('no accounting in either current cycle');
  });

  it('publishes an unqualified figure with BOTH percent clocks stale (#556 acceptance 5)', () => {
    // The state the issue is about. `domain_freshness.hero` used to mean
    // percent-observation age, so this combination kept a staleness marker
    // permanently on. It now means current-cycle accounting resolvability, and
    // the client reads neither axis for combined disclosure.
    updateSnapshot(envWith((b) => {
      const codex = b.sources.codex.data!;
      codex.hero.cycle_freshness = 'stale';
      b.sources.codex.domain_freshness = { hero: 'fresh', quota: 'stale', sessions: 'fresh' };
      b.sources.all.domain_freshness = { hero: 'fresh', quota: 'stale', sessions: 'fresh' };
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    const combined = screen.getByTestId('shared-hero-spent');
    expect(combined).toHaveTextContent('$20.70');
    expect(combined.textContent).not.toMatch(/withheld|unavailable/i);
    expect(screen.queryByTestId('shared-hero-stale-marker')).toBeNull();
    expect(screen.queryByTestId('hero-combined-reason')).toBeNull();
    expect(combined).not.toHaveAttribute('title');
  });

  it('never pairs a published figure with an unavailability sentence (#556 B3)', () => {
    // The exact regression the retired `combined_totals_stale` machinery would
    // reintroduce: hero freshness stale, the source degraded, a hero-domain
    // warning present — and a real number on screen. Deriving disclosure from
    // any of those three would print "Combined totals are unavailable" beside
    // it.
    updateSnapshot(envWith((b) => {
      b.sources.all.availability = 'partial';
      b.sources.all.freshness = 'stale';
      b.sources.all.domain_freshness = { hero: 'stale', quota: 'stale', sessions: 'stale' };
      b.sources.all.warnings = [{
        code: 'claude_week_unresolved',
        message: "Claude's current subscription week could not be resolved.",
        domain: 'hero',
      }];
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    const combined = screen.getByTestId('shared-hero-spent');
    expect(combined).toHaveTextContent('$20.70');
    expect(combined.textContent).not.toMatch(/unavailable|withheld|not published/i);
    expect(screen.queryByTestId('shared-hero-warning')).toBeNull();
  });

  it('states the named reason when the figure is withheld', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.all = {
          ...b.sources.all,
          data: {
            ...b.sources.all.data!,
            combined: null,
            combined_unavailable: {
              code: 'multi_account_unsupported',
              message: 'Claude has 2 accounts on separate cycles, so a combined '
                + 'total is not published; see the per-account cards.',
              causes: [{
                provider: 'claude',
                code: 'multi_account_unsupported',
                detail: { account_count: 2 },
              }],
            },
          },
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    const warning = screen.getByTestId('shared-hero-warning');
    expect(warning).toHaveTextContent('Combined withheld');
    expect(warning).toHaveAttribute('aria-label', expect.stringMatching(/2 accounts/));
    // The reason is VISIBLE, not only in a hover-only title.
    expect(screen.getByTestId('hero-combined-reason'))
      .toHaveTextContent(/per-account cards/);
    // A withheld figure is not "no data": the accounting exists on both sides.
    expect(screen.getByTestId('hero-combined-heading'))
      .toHaveTextContent('COMBINED · CURRENT CYCLES');
  });

  it('prefers the typed reason over any warning on the same entry', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.all = {
          ...b.sources.all,
          warnings: [
            { code: 'projects', message: 'Projects metadata is incomplete.', domain: 'projects' },
            { code: 'other_hero', message: 'A different hero reason.', domain: 'hero' },
          ],
          data: {
            ...b.sources.all.data!,
            combined: null,
            combined_unavailable: {
              code: 'codex_cycle_unavailable',
              message: 'Codex native reset cycle is unavailable.',
              causes: [{ provider: 'codex', code: 'codex_cycle_unavailable' }],
            },
          },
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-combined-reason'))
      .toHaveTextContent('Codex native reset cycle is unavailable.');
  });

  it('falls back to the hero warning for a LEGACY envelope with no typed reason', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.all = {
          ...b.sources.all,
          warnings: [
            { code: 'projects', message: 'Projects metadata is incomplete.', domain: 'projects' },
            { code: 'codex_cycle_unavailable', message: 'Codex native reset cycle is unavailable.', domain: 'hero' },
          ],
          data: { ...b.sources.all.data!, combined: null },
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-combined-reason'))
      .toHaveTextContent('Codex native reset cycle is unavailable.');
  });

  it('names its contributors in the heading and splits the legs in the support zone', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-combined-heading'))
      .toHaveTextContent('COMBINED · CURRENT CYCLES');
    // Criterion 4 — the split is on the hero, without opening a modal.
    expect(screen.getByTestId('hero-leg-claude')).toHaveTextContent('$8.40');
    expect(screen.getByTestId('hero-leg-codex')).toHaveTextContent('$12.30');
    const support = screen.getByTestId('shared-hero-support');
    expect(support).toHaveTextContent('Claude · week to date');
    expect(support).toHaveTextContent('Codex · cycle to date');
    // The duplicated quota rows and the constant `Providers` row are gone.
    expect(support.textContent).not.toContain('Claude · Codex');
    expect(support.textContent).not.toContain('Claude quota');
  });

  it('shows a certified decorated provider subtotal and account count', () => {
    updateSnapshot(envWith((b) => {
      const combined = b.sources.all.data!.combined!;
      combined.legs.claude = {
        state: 'current',
        scope: 'account_cycles',
        cost_usd: 8.4,
        total_tokens: null,
        accounts: [
          {
            account_key: 'a'.repeat(32), cost_usd: 3.4,
            period: {
              kind: 'subscription_week',
              start_at: '2026-04-21T00:00:00Z',
              end_at: '2026-04-28T00:00:00Z',
            },
          },
          {
            account_key: 'b'.repeat(32), cost_usd: 5,
            period: {
              kind: 'subscription_week',
              start_at: '2026-04-22T00:00:00Z',
              end_at: '2026-04-29T00:00:00Z',
            },
          },
        ],
      };
      combined.total_tokens = null;
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-combined-heading'))
      .toHaveTextContent('COMBINED · ACCOUNT CYCLES');
    expect(screen.getByTestId('hero-leg-claude'))
      .toHaveTextContent('$8.40 · 2 accounts');
    expect(screen.getByTestId('shared-hero-support'))
      .toHaveTextContent('Claude · account cycles');
  });

  it('names only the contributing provider when the other leg is empty', () => {
    updateSnapshot(envWith((b) => {
      const combined = b.sources.all.data!.combined!;
      combined.legs.claude = { state: 'empty', cost_usd: 0, total_tokens: 0 };
      combined.cost_usd = combined.legs.codex.cost_usd;
      combined.total_tokens = combined.legs.codex.total_tokens;
      combined.qualifications = [{
        code: 'provider_empty',
        message: 'Claude has no accounting in its current cycle.',
        provider: 'claude',
      }];
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-combined-heading'))
      .toHaveTextContent('CODEX · CURRENT CYCLE');
    expect(screen.getByTestId('hero-leg-claude')).toHaveTextContent('no data');
    expect(screen.getByTestId('hero-leg-codex')).toHaveTextContent('$12.30');
    const qualification = screen.getByTestId('hero-combined-qualification');
    expect(qualification).toHaveTextContent('Claude no data');
    // A qualification is informational: the figure beside it is published and
    // correct. `chip-stale` is the amber freshness vocabulary of the retired
    // `Stale quota` marker, so wearing it re-teaches the category error this
    // session removed.
    expect(qualification.className).not.toContain('chip-stale');
  });

  it('keeps the EMPTY provider\'s own reset beside its percentage', () => {
    // `One provider empty` is a published row of the matrix, so this hero is
    // on screen for real installs — Codex quota observations, no Codex
    // accounting rows, or the mirror image. An `empty` leg names no cycle
    // because the provider contributed nothing, not because its cycle is
    // unknown, so the provider's own reset is still the honest answer. §5's
    // suppression rule is about a CURRENT leg that cannot resolve its bounds.
    const env = envWith((b) => {
      const combined = b.sources.all.data!.combined!;
      combined.legs.claude = { state: 'empty', cost_usd: 0, total_tokens: 0 };
      combined.cost_usd = combined.legs.codex.cost_usd;
      combined.total_tokens = combined.legs.codex.total_tokens;
    });
    // The legacy top-level block is Claude's, and it is where the provider's
    // own server-computed countdown lives.
    (env as { current_week: unknown }).current_week = {
      used_pct: 17.4,
      five_hour_pct: null,
      five_hour_resets_in_sec: null,
      spent_usd: 0,
      dollar_per_pct: null,
      reset_at_utc: '2026-04-28T00:00:00Z',
      reset_in_sec: 216_000,
      last_snapshot_age_sec: 420,
      milestones: [],
      freshness: { label: 'fresh', captured_at: '2026-04-24T13:00:00Z', age_seconds: 420 },
      five_hour_block: null,
    };
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-claude-reset'))
      .toHaveTextContent('Claude resets in 2d 12h');
  });

  it('blanks the figure when BOTH legs are empty, rather than presenting $0', () => {
    updateSnapshot(envWith((b) => {
      const combined = b.sources.all.data!.combined!;
      combined.legs.claude = { state: 'empty', cost_usd: 0, total_tokens: 0 };
      combined.legs.codex = { state: 'empty', cost_usd: 0, total_tokens: 0 };
      combined.cost_usd = 0;
      combined.total_tokens = 0;
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-combined-heading'))
      .toHaveTextContent('CURRENT CYCLES · NO DATA');
    const spent = screen.getByTestId('shared-hero-spent');
    expect(spent.textContent).not.toContain('$0');
    // Not an unavailability either: the figure is not withheld, it is empty.
    expect(screen.queryByTestId('shared-hero-warning')).toBeNull();
  });

  it('keeps a resolved ZERO as ordinary spend inside a named cycle', () => {
    updateSnapshot(envWith((b) => {
      const combined = b.sources.all.data!.combined!;
      combined.legs.claude.cost_usd = 0;
      combined.legs.codex.cost_usd = 0;
      combined.cost_usd = 0;
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-combined-heading'))
      .toHaveTextContent('COMBINED · CURRENT CYCLES');
    expect(screen.getByTestId('shared-hero-spent')).toHaveTextContent('$0');
  });

  it('gives each provider block its own labelled reset (retiring A6)', () => {
    // The server instant must sit INSIDE both fixture cycles. Without it the
    // envelope's periods are years behind the wall clock, both countdowns
    // resolve to elapsed, and this assertion would be measuring the elapsed
    // branch instead of the labelled-reset one it exists for.
    const env = envWith();
    (env as { generated_at: string }).generated_at = '2026-04-24T13:07:00Z';
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.getByTestId('hero-claude-reset')).toHaveTextContent(/^Claude resets in /);
    expect(screen.getByTestId('hero-codex-reset')).toHaveTextContent(/^Codex resets in /);
  });

  it('suppresses only the reset line of a contributing leg with no period', () => {
    // The provider's own quota window still resolves here. The block's
    // countdown must still disappear, because the leg is what the heading and
    // the figure describe, and it cannot name a cycle.
    const env = envWith((b) => {
      delete b.sources.all.data!.combined!.legs.codex.period;
    });
    (env as { generated_at: string }).generated_at = '2026-04-24T13:07:00Z';
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    // It still counts toward the sum and is still named in the heading.
    expect(screen.getByTestId('hero-combined-heading'))
      .toHaveTextContent('COMBINED · CURRENT CYCLES');
    expect(screen.getByTestId('hero-leg-codex')).toHaveTextContent('$12.30');
    expect(screen.queryByTestId('hero-codex-reset')).toBeNull();
    // ONLY that one. Without this the assertion above passes just as well when
    // every countdown is suppressed for an unrelated reason.
    expect(screen.getByTestId('hero-claude-reset'))
      .toHaveTextContent('Claude resets in 3d 10h');
  });

  it('measures both countdowns from the SERVER instant, not the browser clock', () => {
    // A browser clock two days fast used to shorten the All tab's Claude
    // countdown while the Claude tab, which prints the server-computed
    // `reset_in_sec`, kept the right one. Both instants now come from the
    // server, so the two tabs cannot disagree.
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-04-26T13:07:00Z'));
    const env = envWith();
    (env as { generated_at: string }).generated_at = '2026-04-24T13:07:00Z';
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    // Claude's week ends 2026-04-28T00:00Z → 3d 10h from the server instant
    // (1d 10h from the browser's). Codex's cycle ends 2026-04-30T00:00Z.
    expect(screen.getByTestId('hero-claude-reset'))
      .toHaveTextContent('Claude resets in 3d 10h');
    expect(screen.getByTestId('hero-codex-reset'))
      .toHaveTextContent('Codex resets in 5d 10h');
  });

  it('prints no countdown for a cycle that already ended at the server instant', () => {
    // `fmt.ddhh(0)` renders "resets in 0d 0h", which a reader takes as
    // "resetting right now" rather than as evidence that the published bounds
    // are behind the data.
    const env = envWith();
    (env as { generated_at: string }).generated_at = '2026-05-02T00:00:00Z';
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.queryByTestId('hero-claude-reset')).toBeNull();
    expect(screen.queryByTestId('hero-codex-reset')).toBeNull();
  });

  it('renders a hydrating All entry blank, not withheld', () => {
    // `data: null` with no warnings produces no figure AND no reason. Reading
    // "no figure" alone as withheld printed the withheld chip and a sentence
    // claiming a combined total is not published for this state, over a
    // bootstrap that has simply not finished.
    updateSnapshot(envWith((b) => {
      b.sources.all = makeHydratingEntry() as unknown as typeof b.sources.all;
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    expect(screen.queryByTestId('shared-hero-warning')).toBeNull();
    expect(screen.queryByTestId('hero-combined-reason')).toBeNull();
    // Nor the published both-empty sentence: nothing is known yet, so claiming
    // there is no accounting would be a different false statement.
    expect(screen.queryByTestId('hero-combined-no-data')).toBeNull();
    const spent = screen.getByTestId('shared-hero-spent');
    expect(spent).not.toHaveAttribute('title');
    expect(spent.querySelector('.hs-big')).toHaveTextContent('—');
  });

  it('renders both provider blocks at one precision and one size', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<HeroStrip />);

    const nums = Array.from(
      container.querySelectorAll('[data-testid="shared-hero-usage"] .hu-num'),
    );
    expect(nums).toHaveLength(2);
    // A5 — `.hu-num--sm` is gone: two quantities of the same kind, same size.
    expect(nums.every((n) => !n.className.includes('hu-num--sm'))).toBe(true);
    // One precision: `pct1` on both.
    expect(nums.map((n) => n.textContent)).toEqual(['17.4%', '61.0%']);
  });
});

// #556 S1 §5 / acceptance 10 — criterion 9's heading requirement cannot be
// verified by screenshot, so role, level and region name are asserted directly,
// for every source selection.
describe('HeroStrip — region heading (§5)', () => {
  it.each([
    ['all', 'Combined usage summary'],
    ['claude', 'Claude week usage summary'],
    ['codex', 'Codex cycle usage summary'],
  ] as const)('names the %s region and gives it a level-2 heading', (source, name) => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source });
    render(<HeroStrip />);

    expect(screen.getByRole('region', { name })).toBeInTheDocument();
    const heading = screen.getByRole('heading', { level: 2, name });
    expect(heading.tagName).toBe('H2');
    // Visually hidden: nothing about the hero's appearance changes.
    expect(heading.className).toContain('sr-only');
  });
});

describe('HeroStrip — Claude unchanged (default source)', () => {
  it('keeps the subscription-week vocabulary under Claude', () => {
    updateSnapshot(envWith());
    render(<HeroStrip />);
    expect(screen.getByText(/SPENT THIS WEEK/)).toBeInTheDocument();
  });
});
