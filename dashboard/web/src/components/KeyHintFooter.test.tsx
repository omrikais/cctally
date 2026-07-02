import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { KeyHintFooter } from './KeyHintFooter';

describe('KeyHintFooter', () => {
  it('renders each hint with its keys and label, separated by ·', () => {
    render(<KeyHintFooter hints={[
      { keys: <kbd>↑↓</kbd>, label: 'row' },
      { keys: <kbd>Esc</kbd>, label: 'close' },
    ]} />);
    expect(screen.getByText('row')).toBeInTheDocument();
    expect(screen.getByText('close')).toBeInTheDocument();
    expect(document.querySelectorAll('.sep')).toHaveLength(1); // N-1 separators
  });

  it('renders a trailing slot when provided', () => {
    render(<KeyHintFooter hints={[{ keys: <kbd>x</kbd>, label: 'x' }]} trailing={<span data-testid="tail" />} />);
    expect(screen.getByTestId('tail')).toBeInTheDocument();
  });
});
