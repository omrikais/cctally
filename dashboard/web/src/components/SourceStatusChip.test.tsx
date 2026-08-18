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

  it('keeps provider status fresh when only hero and quota domains are stale', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          freshness: 'fresh',
          domain_freshness: {
            hero: 'stale',
            quota: 'stale',
            sessions: 'fresh',
          },
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<SourceStatusChip />);

    const chip = screen.getByTestId('source-status-chip');
    expect(chip).toHaveTextContent('fresh');
    expect(chip).toHaveAttribute('title', 'fresh');
    expect(chip).toHaveAttribute('aria-label', 'codex source status: fresh');
    expect(chip).not.toHaveClass('is-stale');
  });

  it('names a WITHHELD combined figure, and keeps the state word when compact', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.all = {
          ...b.sources.all,
          availability: 'partial',
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
    render(<SourceStatusChip />);

    const chip = screen.getByTestId('source-status-chip');
    expect(chip.querySelector('.source-status-label--full'))
      .toHaveTextContent('Combined withheld');
    // #556 §4.5 — the compact form keeps the STATE word. The retired
    // 'Combined stale' -> 'Combined' mapping dropped it, leaving colour as the
    // only carrier of the state at 390px, where both spans are `aria-hidden`.
    expect(chip.querySelector('.source-status-label--compact'))
      .toHaveTextContent('Withheld');
    expect(chip).toHaveAttribute('title', expect.stringMatching(/2 accounts/));
  });

  it('lets a source-wide warning outrank a permanently withheld figure', () => {
    // Multi-account decoration withholds the combined figure for as long as the
    // install has two accounts, and leaves All `partial` with the providers'
    // own warnings flattened onto it. Ranking the withheld reason first pinned
    // "Combined withheld" on every decorated install and hid every other
    // warning behind it for good.
    updateSnapshot(
      envWith((b) => {
        b.sources.all = {
          ...b.sources.all,
          availability: 'partial',
          warnings: [{
            code: 'codex_metadata_incomplete',
            message: '47 Codex accounting rows lack project metadata; rebuild the cache.',
            domain: 'projects',
          }],
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
    render(<SourceStatusChip />);

    const chip = screen.getByTestId('source-status-chip');
    expect(chip.querySelector('.source-status-label--full'))
      .toHaveTextContent('Projects partial');
    expect(chip.textContent).not.toMatch(/withheld/i);
    // The detail explains the label rather than a second subject.
    expect(chip).toHaveAttribute('title', '47 Codex accounting rows lack project metadata; rebuild the cache.');
  });

  it('says nothing about the combined figure when it is published', () => {
    // The chip agrees with the hero: a published figure is not a degraded one,
    // whatever the source-wide freshness axis says.
    updateSnapshot(
      envWith((b) => {
        b.sources.all = {
          ...b.sources.all,
          domain_freshness: { hero: 'stale', quota: 'stale', sessions: 'stale' },
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<SourceStatusChip />);

    const chip = screen.getByTestId('source-status-chip');
    expect(chip.textContent).not.toMatch(/combined|withheld/i);
  });

  it('shows generic concise copy + degraded style for an unscoped partial/stale warning', () => {
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
    expect(chip).toHaveTextContent('Source degraded');
    expect(chip).toHaveAttribute('title', 'Source ingest is in progress.');
    expect(chip).toHaveClass('is-degraded');
    expect(chip).toHaveAttribute('aria-label', expect.stringContaining('codex source status'));
  });

  it('uses concise domain copy while retaining the full Projects warning for assistive detail', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          availability: 'partial',
          warnings: [{
            code: 'codex_metadata_incomplete',
            message: '47 Codex accounting rows lack project metadata; rebuild the cache.',
            domain: 'projects',
          }],
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<SourceStatusChip />);
    const chip = screen.getByTestId('source-status-chip');
    expect(chip).toHaveTextContent('Projects partial');
    expect(chip.querySelector('.source-status-label--full')).toHaveTextContent('Projects partial');
    expect(chip.querySelector('.source-status-label--compact')).toHaveTextContent('Partial');
    expect(chip).toHaveAttribute('title', '47 Codex accounting rows lack project metadata; rebuild the cache.');
    expect(chip).toHaveAttribute('aria-label', expect.stringContaining('47 Codex accounting rows'));
  });

  it.each([
    ['hero', 'Hero unavailable'],
    ['daily', 'Daily unavailable'],
    ['weekly', 'Weekly unavailable'],
    ['monthly', 'Monthly unavailable'],
    ['sessions', 'Sessions unavailable'],
    ['projects', 'Projects unavailable'],
    ['quota', 'Quota unavailable'],
    ['budget', 'Budget unavailable'],
    ['forensics', 'Forensics unavailable'],
    ['alerts', 'Alerts unavailable'],
  ])('keeps an explicit Unavailable state in the compact %s warning', (domain, fullLabel) => {
    updateSnapshot(
      envWith((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          availability: 'partial',
          warnings: [{
            code: `${domain}_unavailable`,
            message: `${fullLabel}.`,
            domain,
          }],
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<SourceStatusChip />);

    const chip = screen.getByTestId('source-status-chip');
    expect(chip.querySelector('.source-status-label--full')).toHaveTextContent(fullLabel);
    expect(chip.querySelector('.source-status-label--compact')).toHaveTextContent('Unavailable');
    expect(chip).toHaveAttribute('aria-label', `codex source status: ${fullLabel}.`);
  });

  it('prefers a source-wide warning and uses generic visible copy for unknown domains', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.codex = {
          ...b.sources.codex,
          availability: 'partial',
          warnings: [
            { code: 'projects', message: 'Projects only.', domain: 'projects' },
            { code: 'ingest', message: 'The source ingest needs attention.', domain: 'ingest' },
          ],
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<SourceStatusChip />);
    const chip = screen.getByTestId('source-status-chip');
    expect(chip).toHaveTextContent('Source degraded');
    expect(chip).toHaveAttribute('title', 'The source ingest needs attention.');
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
