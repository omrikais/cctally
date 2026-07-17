import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DailyPanel } from './DailyPanel';
import { SessionsPanel } from './SessionsPanel';
import { ProjectsPanel } from './ProjectsPanel';
import { BlocksPanel } from './BlocksPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import fixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function codexEnv(mut?: (b: ReturnType<typeof makeSourceEnvelope>) => void): Envelope {
  const slice = makeSourceEnvelope();
  mut?.(slice);
  return slice as unknown as Envelope;
}

describe('Codex-native panel rendering (§6.2-§6.5)', () => {
  beforeEach(() => {
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  it('Daily renders the native Codex period table (not the Claude heatmap)', () => {
    render(<DailyPanel />);
    const table = screen.getByTestId('codex-period-daily');
    expect(table).toHaveTextContent('04-24');
    expect(table).toHaveTextContent('$12.30');
  });

  it('Sessions renders provider-native rows with native vocabulary', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('codex-sessions-table');
    expect(table).toHaveTextContent('Session 1');
    expect(table).toHaveTextContent('Session 2');
    expect(table).toHaveTextContent('gpt-5-codex');
  });

  it('Projects renders the native qualified-attribution table', () => {
    render(<ProjectsPanel />);
    expect(screen.getByTestId('codex-projects-table')).toHaveTextContent('alpha');
  });

  it('Blocks renders native Codex quota-window labels', () => {
    render(<BlocksPanel />);
    expect(screen.getByTestId('codex-blocks-list')).toHaveTextContent('5-hour limit');
  });
});

describe('All-mode provider-labeled sections (§5.5 Layer 2)', () => {
  beforeEach(() => {
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  it('Daily renders a Claude section AND a Codex section, each with a source chip', () => {
    render(<DailyPanel />);
    // Both provider chips present.
    expect(screen.getByText('Claude', { selector: '.source-chip' })).toBeInTheDocument();
    expect(screen.getByText('Codex', { selector: '.source-chip' })).toBeInTheDocument();
    // The Codex section renders the native table.
    expect(screen.getByTestId('codex-period-daily')).toBeInTheDocument();
  });
});

describe('availability chrome (§5.5 Layer 3)', () => {
  it('Codex hydrating → skeleton', () => {
    updateSnapshot(
      codexEnv((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          availability: 'partial',
          data: null,
          capabilities: {},
          warnings: [],
          last_success_at: null,
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<DailyPanel />);
    expect(screen.getByTestId('panel-source-skeleton')).toBeInTheDocument();
  });

  it('Codex partial/stale → degraded chip beside retained data', () => {
    updateSnapshot(
      codexEnv((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          availability: 'partial',
          freshness: 'stale',
          warnings: [{ code: 'source_ingest_contended', message: 'Source ingest is in progress.' }],
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<DailyPanel />);
    expect(screen.getByText(/Source ingest is in progress/)).toBeInTheDocument();
    // retained data still renders
    expect(screen.getByTestId('codex-period-daily')).toBeInTheDocument();
  });
});
