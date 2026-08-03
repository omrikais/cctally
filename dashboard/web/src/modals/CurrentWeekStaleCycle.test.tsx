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
    // What compose_all_state emits alongside retained combined actuals: an
    // All-LOCAL warning, on the `all` source rather than either provider.
    b.sources.all.warnings = [{
      code: 'combined_totals_stale',
      message: 'Codex quota evidence is stale; combined totals use retained actuals.',
      domain: 'hero',
    }];
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

  it('replaces the per-provider note with an All-local visible marker and reason', () => {
    const { container } = renderFor('all', staleCycleEnv());

    // The per-provider note stays suppressed when embedded (spec §3.7).
    expect(
      container.querySelector('[data-testid="codex-cycle-stale-note"]'),
    ).toBeNull();
    // ...but the All-local reason must appear, or this modal is the one
    // surface that shows a stale-evidence spend figure with zero disclosure.
    const reason = container.querySelector('[data-testid="all-current-week-reason"]');
    expect(reason).not.toBeNull();
    expect(reason!.textContent).toMatch(/combined totals use retained actuals/i);
    const marker = container.querySelector('[data-testid="all-current-week-stale-marker"]');
    expect(marker).toHaveTextContent(/stale quota/i);
    expect(marker).toHaveAttribute('title', expect.stringMatching(/stale/i));
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
    expect(container.querySelector('[data-testid="all-current-week-reason"]'))
      .toHaveTextContent(/combined totals use retained actuals/i);
  });

  it('omits the caveat and its element when the backlog field is absent', () => {
    const { container } = renderFor('codex', envWith());

    expect(
      container.querySelector('[data-testid="codex-ingest-backlog-note"]'),
    ).toBeNull();
  });
});
