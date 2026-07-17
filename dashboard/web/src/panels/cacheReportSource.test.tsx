import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CacheReportPanel } from './CacheReportPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

// A snapshot carrying both the source fields AND the legacy `cache_report` object
// (the Claude forensics legacy fallback, §5.2).
function env(): Envelope {
  return { ...makeSourceEnvelope(), cache_report: { is_empty: true } } as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('CacheReportPanel source labeling (§5.5 layer 2 / §6.6)', () => {
  it('Claude mode renders the panel bare (no provider source chip)', () => {
    updateSnapshot(env());
    render(<CacheReportPanel />);
    expect(screen.getByText('Cache Report', { selector: 'h2' })).toBeInTheDocument();
    expect(screen.queryByText('Claude', { selector: '.source-chip' })).toBeNull();
  });

  it('All mode wraps cache-report in a Claude-labeled provider section', () => {
    updateSnapshot(env());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<CacheReportPanel />);
    // The provider-section chrome now labels it "Claude", matching every other
    // panel in All mode.
    expect(screen.getByText('Claude', { selector: '.source-chip' })).toBeInTheDocument();
    // …wrapping the real cache-report content.
    expect(screen.getByText('Cache Report', { selector: 'h2' })).toBeInTheDocument();
    // No Codex forensics domain → no Codex cache-report section.
    expect(screen.queryByText('Codex', { selector: '.source-chip' })).toBeNull();
  });
});
