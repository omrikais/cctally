import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SourceStatusChip } from './SourceStatusChip';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function envWith(mut?: (b: ReturnType<typeof makeSourceEnvelope>) => void): Envelope {
  const slice = makeSourceEnvelope();
  mut?.(slice);
  return slice as unknown as Envelope;
}

describe('SourceStatusChip (§6.8)', () => {
  it('renders nothing before any snapshot (hydrating)', () => {
    const { container } = render(<SourceStatusChip />);
    expect(container.querySelector('.source-status-chip')).toBeNull();
  });

  it('shows a fresh status for a healthy active source', () => {
    updateSnapshot(envWith());
    render(<SourceStatusChip />);
    const chip = screen.getByTestId('source-status-chip');
    expect(chip).toHaveTextContent('fresh');
    expect(chip).not.toHaveClass('is-degraded');
  });

  it('shows the warning message + degraded style for a partial/stale source', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          availability: 'partial',
          freshness: 'stale',
          warnings: [{ code: 'source_ingest_contended', message: 'Source ingest is in progress.' }],
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<SourceStatusChip />);
    const chip = screen.getByTestId('source-status-chip');
    expect(chip).toHaveTextContent('Source ingest is in progress.');
    expect(chip).toHaveClass('is-degraded');
    expect(chip).toHaveAttribute('aria-label', expect.stringContaining('codex source status'));
  });

  it('shows "no successful snapshot yet" when last_success_at is null', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          availability: 'unavailable',
          data: null,
          capabilities: {},
          warnings: [{ code: 'source_ingest_failed', message: 'Source ingest failed.' }],
          last_success_at: null,
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<SourceStatusChip />);
    expect(screen.getByTestId('source-status-chip')).toHaveTextContent('no successful snapshot yet');
  });

  it('renders nothing in the conversations view', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    const { container } = render(<SourceStatusChip />);
    expect(container.querySelector('.source-status-chip')).toBeNull();
  });
});
