import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Header } from './Header';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import fixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  updateSnapshot(fixture as unknown as Envelope);
});

describe('Header — source selector mount (§5.4)', () => {
  it('renders the source radiogroup in the dashboard view', () => {
    render(<Header />);
    expect(screen.getByRole('radiogroup', { name: /data source/i })).toBeInTheDocument();
  });

  it('hides the source radiogroup in the conversations view', () => {
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<Header />);
    expect(screen.queryByRole('radiogroup', { name: /data source/i })).toBeNull();
  });
});

describe('Header — provider-native condensed readout (§5.4)', () => {
  it('Claude: shows the legacy Used% line when scrolled', () => {
    dispatch({ type: 'SET_HERO_SCROLLED', scrolled: true });
    render(<Header />);
    const c = screen.getByTestId('topbar-condensed');
    expect(c).toHaveAttribute('data-source', 'claude');
    expect(c).toHaveTextContent('resets');
  });

  it('Codex: shows the native quota summary via the label join, never Claude copy', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_HERO_SCROLLED', scrolled: true });
    render(<Header />);
    const c = screen.getByTestId('topbar-condensed');
    expect(c).toHaveAttribute('data-source', 'codex');
    expect(c).toHaveTextContent('5-hour limit');
    // Claude's Used% (17.4) must not leak through under Codex.
    expect(c).not.toHaveTextContent('resets');
  });

  it('All: the condensed readout is hidden', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'SET_HERO_SCROLLED', scrolled: true });
    render(<Header />);
    expect(screen.queryByTestId('topbar-condensed')).toBeNull();
  });
});
