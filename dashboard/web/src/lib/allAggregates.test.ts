import { describe, expect, it } from 'vitest';
import fixture from '../../__tests__/fixtures/envelope.json';
import {
  presentationBlocks,
  presentationDailyLegs,
  presentationDailyRows,
  presentationProjects,
} from './dashboardPresentation';
import type { AllAggregates, Envelope } from '../types/envelope';

// #556 S2 §3.7 — withholding is a TYPED outcome, not an empty list.
//
// Before this session, a range problem reached the user as "No usage history
// yet" (an empty daily array) or "Projects data unavailable — restart the
// dashboard" (a null projects result). The first reports a real failure as
// honest emptiness; the second reports it as a broken instance. Both are
// wrong, and during a v5-server/v6-client overlap both are EXPECTED rather
// than rare, because the in-place update path deliberately leaves an old
// client talking to a restarted server.

function cloneFixture(): Envelope {
  return structuredClone(fixture) as unknown as Envelope;
}

/** A v6 envelope: both rows-only siblings present, both outcomes available. */
function v6Fixture(): Envelope {
  const env = cloneFixture();
  // The fixture leaves `sources.all.data.providers.*` null and relies on
  // `presentationProviders`' documented fallback to the per-source entries, so
  // the rows-only siblings go where that fallback resolves them.
  const claude = env.sources!.claude.data!;
  claude.projects.aggregate = {
    rows: [
      {
        key: 'project:opaque-a', label: 'alpha', source: 'claude',
        cost_usd: 9, sessions_count: 3, drillable: true,
      },
      {
        key: 'project:opaque-b', label: 'beta', source: 'claude',
        cost_usd: 4, sessions_count: 1, drillable: false,
      },
    ],
  };
  claude.periods.daily_aggregate = { rows: env.daily!.rows.map((row) => ({ ...row })) };
  env.sources!.all.data!.aggregates = {
    range: {
      kind: 'absolute_range', label: 'Shared range',
      start_at: '2026-03-26T00:00:00Z', end_at: '2026-04-24T13:07:00Z',
    },
    projects: { state: 'available' },
    daily: { state: 'available' },
  };
  return env;
}

function withAggregates(patch: Partial<AllAggregates>): Envelope {
  const env = v6Fixture();
  env.sources!.all.data!.aggregates = {
    ...env.sources!.all.data!.aggregates!,
    ...patch,
  };
  return env;
}

describe('the All aggregates adapter carries a typed outcome (§3.5.1, §3.7)', () => {
  it('publishes the ranking and the resolved range on an available outcome', () => {
    const projects = presentationProjects(v6Fixture(), 'all');

    expect(projects.state).toBe('available');
    if (projects.state !== 'available') return;
    expect(projects.range?.start_at).toBe('2026-03-26T00:00:00Z');
    expect(projects.range?.end_at).toBe('2026-04-24T13:07:00Z');
    expect(projects.rows.map((row) => row.label)).toContain('alpha');
  });

  it('synthesizes rows_absent when the aggregates object is missing', () => {
    // A v5 server emits no `aggregates` object at all. `rows_absent` is
    // CLIENT-SYNTHESIZED for exactly this: the server cannot emit a code for a
    // field it does not know about.
    const env = v6Fixture();
    delete env.sources!.all.data!.aggregates;

    expect(presentationProjects(env, 'all')).toEqual({
      state: 'withheld', code: 'rows_absent',
    });
    expect(presentationDailyRows(env, 'all')).toEqual({
      state: 'withheld', code: 'rows_absent',
    });
  });

  it('synthesizes rows_absent when an available outcome lacks its rows sibling', () => {
    // The case that matters most. Without it a malformed payload falls through
    // to today's `[]` / `null` and reports as honest emptiness — the exact
    // failure §3.7 exists to prevent.
    const env = v6Fixture();
    delete env.sources!.claude.data!.projects.aggregate;
    delete env.sources!.claude.data!.periods.daily_aggregate;

    expect(presentationProjects(env, 'all')).toEqual({
      state: 'withheld', code: 'rows_absent',
    });
    expect(presentationDailyRows(env, 'all')).toEqual({
      state: 'withheld', code: 'rows_absent',
    });
  });

  it('tolerates an unknown withheld code from a newer server', () => {
    const env = withAggregates({
      projects: { state: 'withheld', code: 'some_future_code' },
    });

    const out = presentationProjects(env, 'all');
    expect(out.state).toBe('withheld');
    if (out.state !== 'withheld') return;
    expect(out.code).toBe('some_future_code');
  });

  it('keeps the provider a provider-scoped code names', () => {
    const env = withAggregates({
      projects: {
        state: 'withheld', code: 'provider_incoherent', provider: 'codex',
      },
      daily: {
        state: 'withheld', code: 'claude_fold_failed', provider: 'claude',
      },
    });

    expect(presentationProjects(env, 'all')).toEqual({
      state: 'withheld', code: 'provider_incoherent', provider: 'codex',
    });
    expect(presentationDailyRows(env, 'all')).toEqual({
      state: 'withheld', code: 'claude_fold_failed', provider: 'claude',
    });
  });

  it('carries a qualification through on a published ranking', () => {
    // Codex metadata partiality stays AVAILABLE with a qualification (§3.7):
    // withholding the whole ranking over it would discard real data.
    const env = withAggregates({
      projects: {
        state: 'available',
        qualifications: [
          { code: 'codex_project_metadata_partial', provider: 'codex' },
        ],
      },
    });

    const out = presentationProjects(env, 'all');
    expect(out.state).toBe('available');
    if (out.state !== 'available') return;
    expect(out.qualifications).toEqual([
      { code: 'codex_project_metadata_partial', provider: 'codex' },
    ]);
  });

  it('withholds one aggregate without withholding its sibling', () => {
    const env = withAggregates({
      projects: { state: 'withheld', code: 'claude_fold_failed', provider: 'claude' },
    });

    expect(presentationProjects(env, 'all').state).toBe('withheld');
    expect(presentationDailyRows(env, 'all').state).toBe('available');
  });

  it('leaves the two single-provider tabs on their own states', () => {
    // The Claude and Codex tabs are untouched by this session. `unavailable`
    // is today's null-envelope state and keeps its own copy; it is NOT a
    // withheld code.
    const env = v6Fixture();
    env.projects = {
      current_week: {
        week_label: null, week_start_date: null, week_start_at: null,
        total_cost_usd: 1,
        rows: [{ key: 'legacy-a', bucket_path: '/w/a', cost_usd: 1, attributed_pct: 4, sessions_count: 1 }],
      },
      trend: { window_weeks: 0, weeks: [], projects: [] },
    };
    expect(presentationProjects(env, 'claude').state).toBe('available');
    expect(presentationDailyRows(env, 'claude').state).toBe('available');

    // A null legacy projects envelope is today's "restart the dashboard"
    // state, and it stays its own thing rather than becoming a withheld code.
    const noProjects = cloneFixture();
    noProjects.projects = null;
    expect(presentationProjects(noProjects, 'claude')).toEqual({
      state: 'unavailable',
    });
  });
});

// #556 S2 §6.1 / §6.2 / §6.3 — what a merged daily row may and may not carry.
describe('the merged All daily row (§6.1, §6.2, §6.3)', () => {
  function withDay(): Envelope {
    const env = v6Fixture();
    const claude = env.sources!.claude.data!;
    claude.periods.daily_aggregate = {
      rows: [{
        source: 'claude', date: '2026-04-24', label: '04-24', cost_usd: 8,
        is_today: true, intensity_bucket: 3,
        models: [{ model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 8, cost_pct: 100 }],
        input_tokens: 10, output_tokens: 5, cache_creation_tokens: 2,
        cache_read_tokens: 3, total_tokens: 20, cache_hit_pct: 20,
      }],
    };
    env.sources!.codex.data!.periods.daily.rows = [{
      label: '2026-04-24', cost_usd: 12, input_tokens: 30,
      cached_input_tokens: 7, output_tokens: 8, reasoning_output_tokens: 2,
      total_tokens: 40, models: ['gpt-5'],
      model_breakdowns: [{ modelName: 'gpt-5', cost: 12 }],
    }];
    return env;
  }

  it('withholds the combined cache ratio, which describes nothing', () => {
    // §6.1 — Claude's ratio is cache-read over input plus cache creation and
    // read; Codex's input is ALREADY cache-inclusive. No ratio over their sum
    // is a quantity. The modal already null-gates the block, so it simply does
    // not render, and the wrong `cacheVocabulary('all')` call goes with it.
    const rows = presentationDailyRows(withDay(), 'all');
    if (rows.state !== 'available') throw new Error('expected available');
    const day = rows.rows.find((row) => row.date === '2026-04-24')!;
    expect(day.cost_usd).toBeCloseTo(20, 9);
    expect(day.cache_hit_pct).toBeNull();
  });

  it('carries no merged chip set across two providers model families', () => {
    // §6.2 — one recomputed stack of Claude and OpenAI models sharing a
    // denominator is not a model split of anything. Chips render per leg.
    const rows = presentationDailyRows(withDay(), 'all');
    if (rows.state !== 'available') throw new Error('expected available');
    const day = rows.rows.find((row) => row.date === '2026-04-24')!;
    expect(day.source).toBe('all');
    expect(day.models).toEqual([]);
  });

  it('supplies the date-matched provider legs the drill-down renders', () => {
    // §6.3 — the breakdown #312 §7.4 requires in exchange for the
    // aggregation, built client-side from rows already in the snapshot.
    const legs = presentationDailyLegs(withDay(), '2026-04-24');
    expect(legs.claude?.cost_usd).toBeCloseTo(8, 9);
    expect(legs.claude?.models.map((m) => m.model)).toEqual(['claude-opus-4-8']);
    expect(legs.codex?.cost_usd).toBeCloseTo(12, 9);
    expect(legs.codex?.models.map((m) => m.model)).toEqual(['gpt-5']);
  });

  it('returns no leg for a provider with no activity that day', () => {
    const legs = presentationDailyLegs(withDay(), '2026-04-23');
    expect(legs.claude).toBeNull();
    expect(legs.codex).toBeNull();
  });
});

// #556 S2 §6.4 — blocks interleave chronologically.
describe('All blocks are one time-ordered list (§6.4)', () => {
  function withBlocks(): Envelope {
    const env = v6Fixture();
    env.sources!.claude.data!.quota.blocks = [
      {
        key: 'block:claude-old', source: 'claude',
        start_at: '2026-04-24T00:00:00Z', end_at: '2026-04-24T05:00:00Z',
        anchor: 'recorded', is_active: false, cost_usd: 1, models: [],
        label: '00:00 Apr 24',
      },
      {
        key: 'block:claude-new', source: 'claude',
        start_at: '2026-04-24T10:00:00Z', end_at: '2026-04-24T15:00:00Z',
        anchor: 'recorded', is_active: true, cost_usd: 3, models: [],
        label: '10:00 Apr 24',
      },
    ];
    env.sources!.codex.data!.quota.blocks = [{
      key: 'block:codex-mid', source: 'codex', label: '05:00 Apr 24',
      window_minutes: 300, start_at: '2026-04-24T05:00:00Z',
      end_at: '2026-04-24T10:00:00Z', resets_at: '2026-04-24T10:00:00Z',
      current_percent: 20, orphaned: false, is_active: false, cost_usd: 2,
      model_breakdowns: [],
    }];
    return env;
  }

  it('orders rows newest-first by real time across providers', () => {
    // The concatenation `[...claude, ...codex]` put every Claude block above
    // every Codex block regardless of when either happened, so a five-hour
    // window from this morning sat below one from last week.
    const rows = presentationBlocks(withBlocks(), 'all');
    expect(rows.map((row) => row.key)).toEqual([
      'block:claude-new', 'block:codex-mid', 'block:claude-old',
    ]);
  });

  it('leaves the single-provider orders alone', () => {
    // The Claude tab reads the LEGACY top-level collection and keeps its
    // envelope order, untouched by this session.
    const env = withBlocks();
    env.blocks = { rows: env.sources!.claude.data!.quota.blocks };
    expect(presentationBlocks(env, 'claude').map((row) => row.key))
      .toEqual(['block:claude-old', 'block:claude-new']);
    expect(presentationBlocks(env, 'codex').map((row) => row.key))
      .toEqual(['block:codex-mid']);
  });
});


// #556 S2 QA P2-10 — the two adapters must apply the `rows_absent` rule to the
// SAME set of required siblings.
describe('the Daily adapter treats Codex as a required sibling too', () => {
  it('withholds when the Codex daily rows object is absent', () => {
    // §3.5.1 names the Codex daily rows a required sibling of the daily
    // aggregate. `presentationProjects` required both providers; the daily
    // adapter checked only Claude and took `providers.codex?.periods.daily
    // .rows ?? []`, so an `available` outcome with no Codex data object
    // published a Claude-only series under the shared-range label with nothing
    // saying Codex was absent.
    const env = v6Fixture();
    delete (env.sources!.codex.data as { periods?: unknown }).periods;

    expect(presentationDailyRows(env, 'all')).toEqual({
      state: 'withheld', code: 'rows_absent',
    });
  });

  it('withholds when the whole Codex data object is absent', () => {
    const env = v6Fixture();
    env.sources!.codex.data = null;

    expect(presentationDailyRows(env, 'all')).toEqual({
      state: 'withheld', code: 'rows_absent',
    });
  });

  it('still treats an EMPTY Codex daily row list as a zero leg, not absence', () => {
    // §3.7 — an empty provider is a zero leg. Only a missing sibling withholds.
    const env = v6Fixture();
    env.sources!.codex.data!.periods.daily.rows = [];

    expect(presentationDailyRows(env, 'all').state).toBe('available');
  });
});

// #556 S2 QA P2-7 — the composition suffix belongs to All alone.
describe('the Codex Projects tab is not "by provider"', () => {
  it('publishes the resolved range on the Codex tab as well as All', () => {
    // The Codex ranking is bounded by the same shared range the aggregate
    // publishes (§3.2: the exclusive upper bound "matches what the Codex
    // projects read already uses"), so the tab can name a period instead of
    // stating none. Before this, only All carried a range and the Codex header
    // fell through to the All-composition branch and read "by provider".
    const out = presentationProjects(v6Fixture(), 'codex');
    expect(out.state).toBe('available');
    if (out.state !== 'available') return;
    expect(out.range?.start_at).toBe('2026-03-26T00:00:00Z');
  });

  it('keeps the Claude tab range-free, because it is not a shared ranking', () => {
    const env = v6Fixture();
    // The fixture ships `projects: null`, which is the Claude tab's
    // `unavailable` state; give it the legacy shape so the range assertion is
    // about the range rather than about the envelope being absent.
    env.projects = {
      current_week: {
        week_label: 'wk Apr 21', week_start_date: '2026-04-21',
        week_start_at: '2026-04-21T00:00:00Z', total_cost_usd: 1, rows: [],
      },
      trend: { window_weeks: 4, weeks: [], projects: [] },
    } as never;

    const out = presentationProjects(env, 'claude');
    expect(out.state).toBe('available');
    if (out.state !== 'available') return;
    expect(out.range).toBeNull();
  });
});
