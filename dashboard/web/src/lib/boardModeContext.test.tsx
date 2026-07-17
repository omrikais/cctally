import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { useContext } from 'react';
import { BoardModeContext } from './boardModeContext';

function Probe() {
  return <span data-testid="mode">{useContext(BoardModeContext)}</span>;
}

describe('BoardModeContext', () => {
  it('defaults to bento (no provider → panels render all rows)', () => {
    render(<Probe />);
    expect(screen.getByTestId('mode').textContent).toBe('bento');
  });
  it('a provider overrides the default', () => {
    render(
      <BoardModeContext.Provider value="stack">
        <Probe />
      </BoardModeContext.Provider>,
    );
    expect(screen.getByTestId('mode').textContent).toBe('stack');
  });
});
