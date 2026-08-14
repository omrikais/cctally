import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, it, expect } from 'vitest';
import { BlocksPanel } from './BlocksPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { BlocksPanelRow, Envelope } from '../types/envelope';
import fixture from '../../__tests__/fixtures/envelope.json';
import {
  ACCOUNT_A,
  makeDecoratedCodexSourceData,
} from '../test-utils/sourceEnvelope';
import { openActiveOrNewestBlockModal } from '../store/actions';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-07-01T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jul 1', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

function blockRow(over: Partial<BlocksPanelRow>): BlocksPanelRow {
  return {
    start_at: '2026-07-01T00:00:00Z', end_at: '2026-07-01T05:00:00Z',
    anchor: 'recorded', is_active: false, cost_usd: 2.0, models: [],
    label: 'Block', ...over,
  };
}

describe('BlocksPanel uncap (#264 S4 A2)', () => {
  it('renders every block row (no 3-cap) so all are reachable via scroll', () => {
    const rows = Array.from({ length: 6 }, (_, i) => blockRow({
      start_at: `2026-07-0${i + 1}T00:00:00Z`,
      end_at: `2026-07-0${i + 1}T05:00:00Z`,
      label: `Block ${i}`,
      cost_usd: (i + 1) * 2,
    }));
    const env = baseEnvelope();
    env.blocks = { rows, total_cost_usd: 42 };
    updateSnapshot(env);
    render(<BlocksPanel />);
    expect(screen.getAllByText(/Block \d/)).toHaveLength(6);
  });
});

describe('BlocksPanel empty-week ⤢ (#265 D)', () => {
  it('disables the expand button when there are no blocks this week', () => {
    updateSnapshot(baseEnvelope()); // blocks.rows === []
    const { container } = render(<BlocksPanel />);
    const expand = container.querySelector('.panel-expand') as HTMLButtonElement;
    expect(expand).not.toBeNull();
    expect(expand.disabled).toBe(true);
  });

  it('leaves the expand button enabled when the week has blocks', () => {
    const env = baseEnvelope();
    env.blocks = { rows: [blockRow({})], total_cost_usd: 2 };
    updateSnapshot(env);
    const { container } = render(<BlocksPanel />);
    expect((container.querySelector('.panel-expand') as HTMLButtonElement).disabled).toBe(false);
  });

  it('keeps Codex Blocks truthfully disabled when no retained 300-minute block exists', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.codex.data!.quota.histories = env.sources!.codex.data!.quota.histories
      .filter((row) => row.window_minutes !== 300);
    env.sources!.codex.data!.quota.blocks = [];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });

    const { container } = render(<BlocksPanel />);

    expect(container.querySelector('[data-source="codex"]')).not.toBeNull();
    expect((container.querySelector('.panel-expand') as HTMLButtonElement).disabled).toBe(true);
    // The scope statement is unchanged; it moved from the h2 onto the
    // wrapping sub-line, because inside the h2 it truncated at 390px.
    expect(container.querySelector('.panel-range-note')!.textContent)
      .toBe('optional 5h · current cycle');
    expect(screen.getByText('No native 5-hour window is currently reported; the 7-day Codex cycle remains available.')).toBeInTheDocument();
  });

  it('keeps native 5-hour framing when Codex reports a 300-minute window', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.codex.data!.quota.blocks = [];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });

    const { container } = render(<BlocksPanel />);

    expect(container.querySelector('.panel-range-note')!.textContent)
      .toBe('5h · current cycle');
    expect(container.querySelector('.panel-range-note')!.textContent)
      .not.toContain('optional');
    expect(screen.getByText('No 5-hour activity blocks in the current Codex cycle.')).toBeInTheDocument();
  });
});

describe('BlocksPanel source-bound detail routing (#319 Task 1)', () => {
  it('All labels each retained five-hour row with its provider owner', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.claude.data!.quota.blocks = [{
      key: 'opaque:server-issued-block-key',
      source: 'claude',
      start_at: '2026-04-24T08:00:00Z',
      end_at: '2026-04-24T13:00:00Z',
      anchor: 'recorded',
      is_active: true,
      cost_usd: 4.2,
      models: [],
      label: '08:00 Apr 24 UTC',
    }];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<BlocksPanel />);

    expect(container.querySelector('.source-chip--claude')).toHaveTextContent('Claude');
    expect(container.querySelector('.source-chip--codex')).toHaveTextContent('Codex');
  });

  it('All names the Codex account in both the chip and accessible row name', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    const codex = makeDecoratedCodexSourceData();
    codex.quota.blocks = [{
      ...codex.quota.blocks[0],
      account_key: ACCOUNT_A,
    }];
    env.sources!.codex.data = codex;
    env.sources!.all.data!.providers.codex = codex;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });

    render(<BlocksPanel />);

    expect(screen.getByTestId('block-account-chip'))
      .toHaveTextContent('work@example.com');
    expect(screen.getByRole('button', {
      name: 'Open detail for work@example.com block starting 13:00 Apr 24 UTC',
    })).toBeInTheDocument();
  });

  it('All keeps a Claude block when the optional Codex five-hour window is absent', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.codex.data!.quota.histories = env.sources!.codex.data!.quota.histories
      .filter((row) => row.window_minutes !== 300);
    env.sources!.codex.data!.quota.blocks = [];
    env.sources!.claude.data!.quota.blocks = [{
      key: 'opaque:server-issued-block-key',
      source: 'claude',
      start_at: '2026-04-24T08:00:00Z',
      end_at: '2026-04-24T13:00:00Z',
      anchor: 'recorded',
      is_active: true,
      cost_usd: 4.2,
      models: [],
      label: '08:00 Apr 24 UTC',
    }];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<BlocksPanel />);

    expect(screen.getByRole('button', { name: 'Open detail for block starting 08:00 Apr 24 UTC' })).toBeInTheDocument();
    expect(container.querySelector('.source-chip--claude')).toHaveTextContent('Claude');
    expect(container.querySelector('.source-chip--codex')).toBeNull();
  });

  it('All routes a Claude-backed row through the canonical Block modal', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.claude.data!.quota.blocks = [{
      key: 'opaque:server-issued-block-key',
      source: 'claude',
      start_at: '2026-04-24T08:00:00Z',
      end_at: '2026-04-24T13:00:00Z',
      anchor: 'recorded',
      is_active: true,
      cost_usd: 4.2,
      models: [],
      label: '08:00 Apr 24 UTC',
    }];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<BlocksPanel />);

    fireEvent.click(screen.getByRole('button', {
      name: 'Open detail for block starting 08:00 Apr 24 UTC',
    }));

    expect(getState().openModal).toBe('block');
    expect(getState().openBlockStartAt).toBe('2026-04-24T08:00:00Z');
    expect(getState().openSourceDetail).toBeNull();
  });

  it('All expand uses the canonical Block modal for its active Claude row', () => {
      // #556 S2 §6.4 — the blocks list interleaves chronologically now, and
      // the "open the active block" affordance takes the FIRST DISPLAYED
      // active row. The fixture's Codex five-hour window starts 13:00, so the
      // Claude block under test starts later than it: this case is about
      // ROUTING a Claude-backed row through the canonical modal, and it must
      // not silently become a test of which provider happens to sort first.
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.claude.data!.quota.blocks = [{
      key: 'opaque:server-issued-block-key',
      source: 'claude',
      start_at: '2026-04-24T18:00:00Z',
      end_at: '2026-04-24T23:00:00Z',
      anchor: 'recorded',
      is_active: true,
      cost_usd: 4.2,
      models: [],
      label: '18:00 Apr 24 UTC',
    }];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<BlocksPanel />);

    fireEvent.click(screen.getByRole('button', { name: 'Open Blocks' }));

    expect(getState().openModal).toBe('block');
    expect(getState().openBlockStartAt).toBe('2026-04-24T18:00:00Z');
    expect(getState().openSourceDetail).toBeNull();
  });
});

// #556 S2 §6.4 — the footer keeps its sum and says what the sum is made of.
describe('#556 S2 — the All blocks footer', () => {
  function allBlocksEnvelope(): Envelope {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.claude.data!.quota.blocks = [{
      key: 'block:claude-a', source: 'claude',
      start_at: '2026-04-24T08:00:00Z', end_at: '2026-04-24T13:00:00Z',
      anchor: 'recorded', is_active: true, cost_usd: 4, models: [],
      label: '08:00 Apr 24 UTC',
    }];
    env.sources!.codex.data!.quota.blocks = [{
      key: 'block:codex-a', source: 'codex', label: '13:00 Apr 24 UTC',
      window_minutes: 300, start_at: '2026-04-24T13:00:00Z',
      end_at: '2026-04-24T18:00:00Z', resets_at: '2026-04-24T18:00:00Z',
      current_percent: 20, orphaned: false, is_active: true, cost_usd: 6,
      model_breakdowns: [],
    }];
    return env;
  }

  beforeEach(() => {
    updateSnapshot(allBlocksEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  it('keeps the summed total and names the per-provider counts and costs', () => {
    // The blocks contract requires the footer to equal the sum of the
    // DISPLAYED rows, so the total stays. What was missing beside it is the
    // attribution, and a statement of what the interleaved list actually spans.
    const { container } = render(<BlocksPanel />);
    const foot = container.querySelector('.panel-foot');
    expect(foot!.textContent).toContain('$10.00');
    expect(foot!.textContent).toContain('Claude 1 block $4.00');
    expect(foot!.textContent).toContain('Codex 1 block $6.00');
  });

  it('states coverage as the displayed interval, never as a shared cycle', () => {
    const { container } = render(<BlocksPanel />);
    const foot = container.querySelector('.panel-foot');
    expect(foot!.textContent).toContain('Apr 24');
    // Two independent five-hour clocks. The footer must not imply the rows
    // form one continuous run or share a reset.
    expect(foot!.textContent).not.toMatch(/continuous|shared cycle|combined cycle/i);
  });

  it('leaves the single-provider footer alone', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<BlocksPanel />);
    expect(container.querySelector('.panel-foot')!.textContent)
      .not.toContain('Claude 1 block');
  });
});


// #556 S2 QA P2-11 — the expand affordance's target changed, and nothing
// pinned it.
//
// `const row = allRows.find((item) => item.is_active) ?? allRows[0]` is
// byte-identical to `main`. What changed underneath it is `presentationBlocks`:
// `[...claude, ...codex]` became a chronological merge, so "the first displayed
// active row" is now the NEWEST active window rather than always a Claude one.
// That is the correct behaviour for a time-ordered list — the affordance opens
// what the eye lands on first — but it is a real routing change on a rule the
// plan listed as unchanged, so it gets a test.
describe('#556 S2 — which block the expand affordance opens under All', () => {
  function withActiveBlocks(claudeStart: string, codexStart: string): Envelope {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.claude.data!.quota.blocks = [{
      key: 'block:claude-active', source: 'claude',
      start_at: claudeStart, end_at: '2026-04-24T23:00:00Z',
      anchor: 'recorded', is_active: true, cost_usd: 4, models: [],
      label: 'Claude active',
    }];
    env.sources!.codex.data!.quota.blocks = [{
      key: 'block:codex-active', source: 'codex', label: 'Codex active',
      window_minutes: 300, start_at: codexStart,
      end_at: '2026-04-24T23:00:00Z', resets_at: '2026-04-24T23:00:00Z',
      current_percent: 20, orphaned: false, is_active: true, cost_usd: 6,
      model_breakdowns: [],
    }];
    return env;
  }

  it('opens the NEWEST active window, which can be the Codex one', () => {
    // Under `[...claude, ...codex]` this opened the Claude block regardless of
    // when either started. Under the time-ordered list the newer Codex window
    // sorts first and is what the affordance targets.
    updateSnapshot(withActiveBlocks('2026-04-24T08:00:00Z', '2026-04-24T18:00:00Z'));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<BlocksPanel />);

    fireEvent.click(screen.getByRole('button', { name: 'Open Blocks' }));

    expect(getState().openSourceDetail).toEqual({
      source: 'codex', resource: 'block', key: 'block:codex-active',
    });
    expect(getState().openModal).not.toBe('block');
  });

  it('opens the Claude one when IT is the newest, through the canonical modal', () => {
    updateSnapshot(withActiveBlocks('2026-04-24T18:00:00Z', '2026-04-24T08:00:00Z'));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<BlocksPanel />);

    fireEvent.click(screen.getByRole('button', { name: 'Open Blocks' }));

    expect(getState().openModal).toBe('block');
    expect(getState().openBlockStartAt).toBe('2026-04-24T18:00:00Z');
    expect(getState().openSourceDetail).toBeNull();
  });

  it('leaves the 7-key shortcut on the legacy Claude blocks collection', () => {
    // `openActiveOrNewestBlockModal` reads `snapshot.blocks.rows`, the
    // top-level Claude panel, NOT `presentationBlocks`. The ordering change
    // cannot reach it, so the digit shortcut still opens a Claude block under
    // every selection.
    const env = withActiveBlocks('2026-04-24T08:00:00Z', '2026-04-24T18:00:00Z');
    env.blocks = { rows: [{
      start_at: '2026-04-24T08:00:00Z', end_at: '2026-04-24T13:00:00Z',
      anchor: 'recorded', is_active: true, cost_usd: 4, models: [],
      label: '08:00 Apr 24 UTC',
    }] } as never;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });

    openActiveOrNewestBlockModal();

    expect(getState().openModal).toBe('block');
    expect(getState().openBlockStartAt).toBe('2026-04-24T08:00:00Z');
    expect(getState().openSourceDetail).toBeNull();
  });
});


// #556 S2 QA — Blocks was the worst-truncated header on the board. Measured at
// 390px before this change: clientWidth 126 against scrollWidth 267, 47%
// visible, so `Blocks (5h · current provider cycles)` rendered as "Blocks (5h ·
// curren…" and the composition — which cycles the rows come from — was the
// part cut away. The whole scope statement moves, unshortened, onto
// `.panel-range-note`, the wrapping full-width sub-line Projects introduced.
// It moves WHOLE rather than splitting unit-in-h2 / cycle-on-the-note, because
// `Blocks (optional 5h)` alone still measured 161px against the 126px the
// actions cluster leaves — 78%, clipping the unit.
describe('#556 S2 QA — the Blocks composition is off the h2', () => {
  it('states the provider cycles on the sub-line under All, not in the h2', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });

    const { container } = render(<BlocksPanel />);

    expect(container.querySelector('.panel-header h2')!.textContent!.trim())
      .toBe('Blocks');
    expect(container.querySelector('.panel-range-note')!.textContent)
      .toBe('5h · current provider cycles');
  });

  it('states the Claude week on the sub-line too, so one panel has one pattern', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });

    const { container } = render(<BlocksPanel />);

    expect(container.querySelector('.panel-header h2')!.textContent!.trim())
      .toBe('Blocks');
    expect(container.querySelector('.panel-range-note')!.textContent)
      .toBe('5h · current week');
  });

  it('renders the sub-line as a SIBLING of the header, which is what lets it wrap', () => {
    updateSnapshot(structuredClone(fixture) as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<BlocksPanel />);
    expect(container.querySelector('.panel-header .panel-range-note')).toBeNull();
    const note = container.querySelector('.panel-range-note')!;
    expect(note.parentElement!.querySelector(':scope > .panel-header')).not.toBeNull();
  });
});
