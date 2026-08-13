// #350 — the Codex current-week modal renders cost, percent and cycle bounds
// without consulting freshness, and its `providerReason` checks only warnings
// and cycle nullity. A stale-but-valid cycle is therefore invisible there
// unless it is disclosed explicitly (spec §3.7). The All modal shows only the
// All-local §3.5 reason.
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { DashboardSelection, Envelope } from '../types/envelope';
import { CurrentWeekModal } from './CurrentWeekModal';

function envWith(mut?: (b: ReturnType<typeof makeSourceEnvelope>) => void): Envelope {
  const slice = makeSourceEnvelope();
  mut?.(slice);
  return {
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
  } as unknown as Envelope;
}

function staleCycleEnv(): Envelope {
  return envWith((b) => {
    const codex = b.sources.codex.data!;
    (codex.hero as unknown as { cycle_freshness?: string }).cycle_freshness = 'stale';
    const weekly = codex.quota.histories.find((row) => row.window_minutes === 10_080)!;
    weekly.freshness = 'stale';
    weekly.forecast.status = 'stale';
    b.sources.codex.domain_freshness = { hero: 'stale', quota: 'stale', sessions: 'fresh' };
    b.sources.all.domain_freshness = { hero: 'stale', quota: 'stale', sessions: 'fresh' };
    b.sources.all.availability = 'partial';
    // #556 S1 §4.2 — `combined_totals_stale` is retired. Stale Codex quota
    // evidence does not withhold the figure and does not qualify it: the cycle
    // still resolves and the actuals inside it are correct. What the modal must
    // now show for this state is NOTHING about the combined figure.
  });
}

// The state that DOES withhold the figure, so the modal has a reason to state.
function withheldCombinedEnv(): Envelope {
  return envWith((b) => {
    b.sources.all.availability = 'partial';
    b.sources.all.data!.combined = null;
    b.sources.all.data!.combined_unavailable = {
      code: 'multi_account_unsupported',
      message: 'Claude has 2 accounts on separate cycles, so a combined total '
        + 'is not published; see the per-account cards.',
      causes: [{
        provider: 'claude',
        code: 'multi_account_unsupported',
        detail: { account_count: 2 },
      }],
    };
  });
}

const BACKLOG = { files: 3, bytes: 8192, since: '2026-08-02T20:30:00Z' };

function withBacklog(env: Envelope): Envelope {
  const next = structuredClone(env);
  next.sources!.codex.data!.ingest_backlog = BACKLOG;
  return next;
}

function decoratedBacklogEnv(): Envelope {
  return envWith((b) => {
    b.sources.codex.data = {
      ...makeDecoratedCodexSourceData(),
      ingest_backlog: BACKLOG,
    };
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

describe('CurrentWeekModal — stale Codex cycle disclosure (#350)', () => {
  it('shows a stale note on the Codex modal while keeping spend and bounds', () => {
    const { container } = renderFor('codex', staleCycleEnv());

    const note = container.querySelector('[data-testid="codex-cycle-stale-note"]');
    expect(note).not.toBeNull();
    expect(note!.textContent).toMatch(/stale/i);
    // The actuals are untouched — the modal still renders spend and the cycle.
    expect(container.querySelector('.mcw-mini')!.textContent).toContain('$12.30');
    expect(container.querySelector('.m-pill')!.textContent).toContain('Apr 23');
  });

  it('omits the note when the Codex cycle is fresh', () => {
    const { container } = renderFor('codex', envWith());

    expect(
      container.querySelector('[data-testid="codex-cycle-stale-note"]'),
    ).toBeNull();
    expect(container.querySelector('.mcw-mini')!.textContent).toContain('$12.30');
  });

  it('says nothing about the combined figure while stale evidence bounds it', () => {
    const { container } = renderFor('all', staleCycleEnv());

    // The per-provider note stays suppressed when embedded (spec §3.7).
    expect(
      container.querySelector('[data-testid="codex-cycle-stale-note"]'),
    ).toBeNull();
    // #556 B2/B3 — the figure is published and correct, so pairing it with an
    // unavailability sentence would be false. This is the modal half of the
    // hero's own assertion.
    expect(
      container.querySelector('[data-testid="all-current-week-reason"]'),
    ).toBeNull();
  });

  it('states the named reason when the figure is WITHHELD', () => {
    const { container } = renderFor('all', withheldCombinedEnv());

    const reason = container.querySelector('[data-testid="all-current-week-reason"]');
    expect(reason).not.toBeNull();
    expect(reason!.textContent).toMatch(/2 accounts on separate cycles/i);
    const marker = container.querySelector(
      '[data-testid="all-current-week-withheld-marker"]');
    expect(marker).toHaveTextContent(/combined withheld/i);
    expect(marker).toHaveAttribute('title', expect.stringMatching(/2 accounts/));
  });

  it('renders no All-local reason when both provider cycles are fresh', () => {
    const { container } = renderFor('all', envWith());

    expect(
      container.querySelector('[data-testid="all-current-week-reason"]'),
    ).toBeNull();
  });
});

describe('CurrentWeekModal — Codex ingest backlog disclosure (#456)', () => {
  it('shows the full caveat in the ordinary Codex current-cycle modal', () => {
    const { container } = renderFor('codex', withBacklog(envWith()));
    const note = container.querySelector('[data-testid="codex-ingest-backlog-note"]');

    expect(note).not.toBeNull();
    expect(note!.textContent).toMatch(/3 sessions left/i);
    expect(note!.textContent).toMatch(/totals will rise/i);
    expect(note!.className).toContain('mcw-ms-sub');
    expect(container.querySelector('.mcw-mini')!.textContent).toContain('$12.30');
  });

  it('shows the caveat before the decorated All-accounts table', () => {
    const { container } = renderFor('codex', decoratedBacklogEnv());
    const note = container.querySelector('[data-testid="codex-ingest-backlog-note"]');
    const table = container.querySelector('[data-testid="codex-cycle-per-account"]');

    expect(note).not.toBeNull();
    expect(note!.textContent).toMatch(/still loading/i);
    expect(table).not.toBeNull();
    expect(
      note!.compareDocumentPosition(table!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('propagates the store-wide caveat into the embedded All modal section', () => {
    const { container } = renderFor('all', withBacklog(staleCycleEnv()));
    const codexSection = container.querySelector('[data-provider-section="codex"]')!;

    expect(
      codexSection.querySelector('[data-testid="codex-ingest-backlog-note"]'),
    ).toHaveTextContent(/still loading/i);
    expect(
      codexSection.querySelector('[data-testid="codex-cycle-stale-note"]'),
    ).toBeNull();
  });

  it('states the combined qualification from the wire, not the provider field', () => {
    // #556 §4.3 inverse pair, at the modal. The provider field carries the
    // backlog for the Codex section's own note; the All-level qualification is
    // the one composition lifted, and only that reaches the combined figure.
    const withQualification = withBacklog(envWith());
    withQualification.sources!.all.data!.combined!.qualifications = [{
      code: 'codex_ingest_backlog',
      message: 'Codex has pending accounting to ingest, so its cycle total may '
        + 'be incomplete.',
      provider: 'codex',
    }];
    const { container } = renderFor('all', withQualification);

    expect(container.querySelector('[data-testid="all-current-week-qualification"]'))
      .toHaveTextContent(/pending accounting/i);
  });

  it('renders no combined qualification carried only by the provider field', () => {
    const { container } = renderFor('all', withBacklog(envWith()));

    expect(
      container.querySelector('[data-testid="all-current-week-qualification"]'),
    ).toBeNull();
  });

  it('omits the caveat and its element when the backlog field is absent', () => {
    const { container } = renderFor('codex', envWith());

    expect(
      container.querySelector('[data-testid="codex-ingest-backlog-note"]'),
    ).toBeNull();
  });
});
