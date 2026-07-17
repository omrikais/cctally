import { beforeEach, describe, expect, it } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import { SessionsPanel } from './SessionsPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

function bundleEnv(): Envelope {
  return makeSourceEnvelope() as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('SourceSessionsGrid — Codex columns + vocabulary (§6.3)', () => {
  beforeEach(() => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  it('renders the full panel chrome (section + collapse toggle), not a bare shell table', () => {
    render(<SessionsPanel />);
    expect(document.getElementById('panel-sessions')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Collapse Recent Sessions|Expand Recent Sessions/ }),
    ).toBeInTheDocument();
  });

  it('renders the enumerated per-source columns (five token cells + recency + cost)', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('codex-sessions-table');
    for (const label of ['Session', 'Last activity', 'Models', 'Input', 'Cached', 'Output', 'Reasoning', 'Total', 'Cost']) {
      expect(table).toHaveTextContent(label);
    }
    // Native token values render (Session 1 total 276000 → formatted).
    expect(table).toHaveTextContent('Session 1');
    expect(table).toHaveTextContent('gpt-5-codex'); // Session 2's model chip
  });

  it('is a roving grid (role=grid, exactly one body tab stop)', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('codex-sessions-table');
    expect(table).toHaveAttribute('role', 'grid');
    const rows = within(table)
      .getAllByRole('row')
      .filter((r) => r.classList.contains('source-session-row'));
    expect(rows).toHaveLength(2);
    expect(rows.filter((r) => r.getAttribute('tabindex') === '0')).toHaveLength(1);
  });

  it('the Session cell opens the qualified Codex detail (source-aware, not the legacy route)', () => {
    render(<SessionsPanel />);
    const btn = screen.getAllByRole('button', { name: /Open codex session details/ })[0];
    act(() => {
      btn.click();
    });
    expect(getState().openSourceDetail).toEqual({
      source: 'codex',
      resource: 'session',
      key: 'session:codex-a',
    });
  });

  it('a sortable header click dispatches SET_SOURCE_SESSIONS_SORT', () => {
    render(<SessionsPanel />);
    const costHeader = screen.getByText('Cost', { selector: '.th-label' });
    act(() => {
      costHeader.click();
    });
    expect(getState().sourceSessionsSort).toEqual({ column: 'cost', direction: 'desc' });
  });

  it('a search needle marks matched codex rows (highlight aligns with rendered order)', () => {
    render(<SessionsPanel />);
    act(() => {
      dispatch({ type: 'SET_SEARCH', text: 'Session 2' });
    });
    const rows = within(screen.getByTestId('codex-sessions-table'))
      .getAllByRole('row')
      .filter((r) => r.classList.contains('source-session-row'));
    // Only the matching row carries .search-match.
    expect(rows.filter((r) => r.classList.contains('search-match'))).toHaveLength(1);
    expect(rows[1].classList.contains('search-match')).toBe(true); // codex-b
  });
});

describe('SourceSessionsGrid — All-mode interleave (§6.3)', () => {
  beforeEach(() => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  it('renders one interleaved grid with a per-row source chip (both providers present)', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('source-sessions-table');
    const chipLabels = within(table)
      .getAllByText(/Claude|Codex/, { selector: '.source-chip' })
      .map((c) => c.textContent);
    expect(chipLabels).toContain('Claude');
    expect(chipLabels).toContain('Codex');
    // Three interleaved rows (2 codex + 1 claude), recency-ordered.
    const rows = within(table)
      .getAllByRole('row')
      .filter((r) => r.classList.contains('source-session-row'));
    expect(rows).toHaveLength(3);
    expect(rows[2].getAttribute('data-detail-source')).toBe('claude'); // oldest
  });
});
