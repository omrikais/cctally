import { beforeEach, describe, expect, it } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { DailyPanel } from './DailyPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import fixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope } from '../types/envelope';

// §5.1 re-key proof: panel subtrees re-key by source on switch, so no
// intermediate mixed render survives (no cross-generation relabel). Rendering a
// panel under one source and dispatching SET_ACTIVE_SOURCE in place must leave
// zero stale text from the prior source in the very next frame.

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('panel re-key on source switch (§5.1)', () => {
  it('Claude → Codex: no stale Claude-sourced text survives the switch frame', () => {
    updateSnapshot(fixture as unknown as Envelope);
    render(<DailyPanel />);
    // Claude-only heatmap chrome is present under Claude.
    expect(screen.getByText(/heatmap · 30 days/)).toBeInTheDocument();
    act(() => {
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    });
    // The Claude subtree unmounted; only the Codex-native table remains.
    expect(screen.queryByText(/heatmap · 30 days/)).toBeNull();
    expect(screen.getByTestId('codex-period-daily')).toBeInTheDocument();
  });

  it('Codex → Claude: no stale Codex-sourced table survives the switch frame', () => {
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<DailyPanel />);
    expect(screen.getByTestId('codex-period-daily')).toBeInTheDocument();
    act(() => {
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    });
    expect(screen.queryByTestId('codex-period-daily')).toBeNull();
    expect(screen.getByText(/heatmap · 30 days/)).toBeInTheDocument();
  });
});
