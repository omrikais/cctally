import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DailyPanel } from './DailyPanel';
import { WeeklyPanel } from './WeeklyPanel';
import { MonthlyPanel } from './MonthlyPanel';
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

  it('Daily renders the canonical heatmap with Codex-adapted values', () => {
    const { container } = render(<DailyPanel />);
    expect(container.querySelector('.daily-cal-grid')).toBeInTheDocument();
    expect(screen.getAllByText('$12.30').length).toBeGreaterThan(0);
    expect(container.querySelector('.codex-period-table')).toBeNull();
  });

  it('Sessions renders provider-native rows with native vocabulary', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('codex-sessions-table');
    expect(table).toHaveTextContent('Session 1');
    expect(table).toHaveTextContent('Session 2');
    expect(table).toHaveTextContent('gpt-5-codex');
  });

  it('Projects renders the canonical leaderboard', () => {
    const { container } = render(<ProjectsPanel />);
    expect(container.querySelector('.projects-row')).toHaveTextContent('alpha');
    expect(container.querySelector('.codex-projects-table')).toBeNull();
  });

  it('Blocks renders native 5-hour activity with model-split canonical gauges', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.codex.data!.quota.blocks[0] = {
      ...env.sources!.codex.data!.quota.blocks[0],
      window_minutes: 300,
      start_at: '2026-04-24T13:00:00Z',
      cost_usd: 12.3,
      model_breakdowns: [
        { modelName: 'gpt-5.6-sol', cost: 8 },
        { modelName: 'gpt-5.6-terra', cost: 4.3 },
      ],
    };
    updateSnapshot(env);
    const { container } = render(<BlocksPanel />);
    expect(container.querySelector('.blocks-row')).toHaveTextContent('gpt-5.6-sol');
    expect(container.querySelector('.blocks-row')).toHaveTextContent('gpt-5.6-terra');
    expect(container.querySelector('.gauge-track')).toBeInTheDocument();
    expect(container.querySelector('.panel-foot')).toHaveTextContent('$12.30');
  });

  it('Blocks uses the canonical empty state when Codex has no 5-hour blocks', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.codex.data!.quota.blocks = [{
      key: 'block:weekly-only', source: 'codex', label: '7-day limit',
      window_minutes: 10_080, start_at: '2026-04-23T00:00:00Z',
      end_at: '2026-04-30T00:00:00Z', resets_at: '2026-04-30T00:00:00Z',
      current_percent: 61, orphaned: false, is_active: true,
      cost_usd: 0, model_breakdowns: [],
    }];
    updateSnapshot(env);

    const { container } = render(<BlocksPanel />);
    expect(container.querySelector('.blocks-row')).toBeNull();
    expect(container.querySelector('.panel-empty')).toHaveTextContent(
      'No 5-hour activity blocks in the current Codex cycle.',
    );
  });

  it.each([
    ['daily', DailyPanel],
    ['weekly', WeeklyPanel],
    ['monthly', MonthlyPanel],
    ['projects', ProjectsPanel],
    ['blocks', BlocksPanel],
  ] as const)('renders %s in one canonical panel frame', (_kind, Panel) => {
    const { container, unmount } = render(<Panel />);
    const frame = container.querySelector('.panel');
    expect(frame).toBeInTheDocument();
    expect(frame?.querySelector('.panel-header h2')).toBeInTheDocument();
    expect(frame?.querySelector('.panel-header .panel-grip')).toBeInTheDocument();
    expect(frame?.querySelector(':scope > .panel-body')).toBeInTheDocument();
    expect(container.querySelector('.panel-source-codex')).toBeNull();
    unmount();
  });
});

describe('All-mode provider-labeled sections (§5.5 Layer 2)', () => {
  beforeEach(() => {
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  it('Daily renders one canonical shell with combined provider values', () => {
    const { container } = render(<DailyPanel />);
    expect(container.querySelectorAll('.panel')).toHaveLength(1);
    expect(container.querySelector('.daily-cal-grid')).toBeInTheDocument();
    expect(container.querySelector('.source-provider-section')).toBeNull();
    expect(container.querySelector('.panel .panel')).toBeNull();
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
    expect(document.querySelector('.panel-skeleton')).toBeInTheDocument();
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
    expect(screen.getByRole('status', { name: /Source ingest is in progress/ })).toHaveTextContent('partial');
    // retained data still renders
    expect(document.querySelector('.daily-cal-grid')).toBeInTheDocument();
  });

  it.each([
    ['missing', undefined],
    ['ingest', 'ingest'],
    ['read_model', 'read_model'],
    ['unknown', 'future-domain'],
  ] as const)(
    'shows a %s source-wide warning on Daily and Projects',
    (label, domain) => {
      updateSnapshot(
        codexEnv((b) => {
          b.sources.codex = {
            ...b.sources.codex,
            availability: 'partial',
            freshness: 'fresh',
            warnings: [{
              code: `source_${label}`,
              message: `Source-wide ${label} warning.`,
              ...(domain === undefined ? {} : { domain }),
            }],
          };
        }),
      );
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });

      const daily = render(<DailyPanel />);
      const projects = render(<ProjectsPanel />);

      expect(daily.container.querySelector('.panel-degraded-chip')).toHaveAccessibleName(
        `Degraded: Source-wide ${label} warning.`,
      );
      expect(projects.container.querySelector('.panel-degraded-chip')).toHaveAccessibleName(
        `Degraded: Source-wide ${label} warning.`,
      );
    },
  );

  it('keeps a projects-only warning scoped to Projects', () => {
    updateSnapshot(
      codexEnv((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          availability: 'partial',
          freshness: 'fresh',
          warnings: [{
            code: 'codex_metadata_incomplete',
            message: 'Project metadata is incomplete.',
            domain: 'projects',
          }],
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });

    const daily = render(<DailyPanel />);
    const projects = render(<ProjectsPanel />);

    expect(daily.container.querySelector('.panel-degraded-chip')).toBeNull();
    expect(projects.container.querySelector('.panel-degraded-chip')).toHaveAccessibleName(
      'Degraded: Project metadata is incomplete.',
    );
  });
});
