// #416 QA/review B1 — the EXPANDED view of a scoped panel must stay scoped.
//
// Every panel these modals expand was converted to `useScopedSnapshot()`, but
// the modals themselves kept plain `useSnapshot()`. Because each of those
// panels ships an `ExpandButton`, one click took the operator from the focused
// account's rows to the MERGED all-account rows — under the focused account's
// chip, with no disclosure. For an account with no activity the panel showed
// the honest empty state while its own expansion showed every other account's
// data.
//
// These tests pin the DATA BINDING only: which rows the expansion paints. The
// fixture children are marker-prefixed (`B-04-24`, `proj-B`) and the merged
// parent is not, so a modal reading the parent is visible as a missing marker
// or as a parent-only total. jsdom cannot evaluate @media, real scroll or
// trusted pointer events — visual/interaction correctness stays the real-browser
// QA gate's job.
import { beforeEach, describe, expect, it } from 'vitest';
import { act, cleanup, render } from '@testing-library/react';
import { DailyModal } from './DailyModal';
import { WeeklyModal } from './WeeklyModal';
import { MonthlyModal } from './MonthlyModal';
import { ProjectsModal } from './ProjectsModal';
import { TrendModal } from './TrendModal';
import { CacheReportModal } from './CacheReportModal';
import { ForecastModal } from './ForecastModal';
import { CurrentWeekModal } from './CurrentWeekModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  ACCOUNT_A,
  ACCOUNT_B,
  ACCOUNT_EMPTY,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { CodexSourceData, Envelope } from '../types/envelope';

function decoratedEnv(): Envelope {
  const slice = makeSourceEnvelope() as unknown as {
    sources: { codex: { data: CodexSourceData } };
  };
  slice.sources.codex.data = makeDecoratedCodexSourceData();
  return slice as unknown as Envelope;
}

// Open `kind` the way the panel's ExpandButton does: bind the modal source at
// open time, with the Codex account chip already focused.
function openScoped(kind: string, account: string | null): void {
  act(() => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    if (account != null) {
      dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account });
    }
    dispatch({ type: 'OPEN_MODAL', kind } as never);
  });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
});

describe('Period modals expand the focused account, not the merged parent', () => {
  it('daily paints the focused account day rows and not the merged total', () => {
    openScoped('daily', ACCOUNT_B);
    const { container } = render(<DailyModal />);
    expect(container.textContent).toContain('B-04-24');
    // $12.30 is the MERGED day (8.00 + 4.30) — account B alone spent $4.30.
    expect(container.textContent).not.toContain('$12.30');
  });

  it('weekly paints the focused account cycle rows', () => {
    openScoped('weekly', ACCOUNT_B);
    const { container } = render(<WeeklyModal />);
    expect(container.textContent).toContain('B-04-24');
    // $20.10 is the merged weekly row the parent carries.
    expect(container.textContent).not.toContain('$20.10');
  });

  it('monthly paints the focused account month rows', () => {
    openScoped('monthly', ACCOUNT_B);
    const { container } = render(<MonthlyModal />);
    expect(container.textContent).toContain('B-04-24');
    // $38.50 is the merged month the parent carries.
    expect(container.textContent).not.toContain('$38.50');
  });

  it('still paints the merged parent under All accounts', () => {
    openScoped('daily', null);
    const { container } = render(<DailyModal />);
    expect(container.textContent).toContain('$12.30');
    expect(container.textContent).not.toContain('B-04-24');
  });
});

describe('Projects modal expands the focused account', () => {
  it('lists that account projects only', () => {
    openScoped('projects', ACCOUNT_B);
    const { container } = render(<ProjectsModal />);
    expect(container.textContent).toContain('proj-B');
    expect(container.textContent).not.toContain('alpha');
  });
});

describe('Trend modal expands the focused account', () => {
  it('charts that account cycle history', () => {
    openScoped('trend', ACCOUNT_B);
    const { container } = render(<TrendModal />);
    expect(container.textContent).toContain('B-04-24');
    // $20.10 is the merged weekly row.
    expect(container.textContent).not.toContain('$20.10');
  });
});

describe('Cache report modal expands the focused account', () => {
  it('tabulates that account days', () => {
    openScoped('cache-report', ACCOUNT_B);
    const { container } = render(<CacheReportModal />);
    // Non-vacuity: the modal really did render its daily table.
    expect(container.textContent).toContain('Daily rows');
    // 480K input tokens is the MERGED day; account B's own day carries 10.
    expect(container.textContent).not.toContain('480K');
  });
});

describe('Forecast modal expands the focused account', () => {
  it('does not project from the merged parent quota history', () => {
    openScoped('forecast', ACCOUNT_B);
    const { container } = render(<ForecastModal />);
    // Non-vacuity: the modal really did render its projection block.
    expect(container.textContent).toContain('Projected @ reset');
    // The parent's representative weekly history projects to 104.0% at reset;
    // account B's own child carries B's window (31.0%), so the projection is
    // that account's rather than the representative's.
    expect(container.textContent).not.toContain('104.0%');
    expect(container.textContent).toContain('31.0%');
  });

  // #416 QA P0 — this assertion used to read "still projects from the merged
  // parent under All accounts", and that WAS the defect: the parent's weekly
  // history list carries one row per account, so "the merged parent's
  // projection" is whichever account sorts first. The modal now discloses every
  // account's own projection instead of electing one.
  it('discloses every account rather than projecting from a representative', () => {
    openScoped('forecast', null);
    const { container } = render(<ForecastModal />);
    expect(container.querySelector('[data-testid="codex-forecast-per-account"]'))
      .not.toBeNull();
    const rows = [...container.querySelectorAll('[data-testid="forecast-account-row"]')];
    expect(rows.map((el) => el.getAttribute('data-account-key')))
      .toEqual([ACCOUNT_A, ACCOUNT_B, ACCOUNT_EMPTY]);
    // The representative's 104.0% is still SHOWN — on its own labelled row,
    // never as the provider's number.
    expect(rows[0].textContent).toContain('104.0%');
    expect(rows[0].textContent).toContain('work@example.com');
  });
});

// #416 QA P0-B — the cycle modal opened from the "All accounts" hero.
//
// The hero already blanks percent and reset under All accounts precisely
// because independent quota allowances are never blended (D6). The modal
// opened from that hero was still rendering ONE representative account's
// percentage ladder — pill, big number, $/1%, "N crossed" and every milestone
// row — with no account named anywhere in the dialog. Blending the crossings
// instead would violate D6 outright, so the honest state is per-account.
describe('The All-accounts cycle modal is per-account, never one account ladder', () => {
  it('names every account and drops the representative milestone ladder', () => {
    openScoped('current-week', null);
    const { container } = render(<CurrentWeekModal />);
    expect(container.querySelector('[data-testid="codex-cycle-per-account"]'))
      .not.toBeNull();
    expect(container.textContent).toContain('work@example.com');
    expect(container.textContent).toContain('personal@example.com');
    expect(container.textContent).toContain('quiet@example.com');
    // "N crossed" is the milestone-ladder header: its presence means one
    // account's ladder is being presented as the merged truth.
    expect(container.textContent).not.toContain('crossed');
  });

  it('gives a focused account its own ladder back', () => {
    openScoped('current-week', ACCOUNT_A);
    const { container } = render(<CurrentWeekModal />);
    expect(container.querySelector('[data-testid="codex-cycle-per-account"]'))
      .toBeNull();
    expect(container.textContent).toContain('crossed');
  });
});
