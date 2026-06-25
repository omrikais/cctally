import { useRef, useState } from 'react';
import { render, fireEvent, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { useOutsideDismiss } from './useOutsideDismiss';

function Harness() {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(true);
  useOutsideDismiss(ref, open, () => setOpen(false));
  return (
    <div>
      <div ref={ref} data-testid="box">{open ? 'open' : 'closed'}<button>inside</button></div>
      <button data-testid="outside">outside</button>
    </div>
  );
}

describe('useOutsideDismiss', () => {
  it('dismisses on pointerdown outside the ref', () => {
    render(<Harness />);
    expect(screen.getByTestId('box')).toHaveTextContent('open');
    fireEvent.pointerDown(screen.getByTestId('outside'));
    expect(screen.getByTestId('box')).toHaveTextContent('closed');
  });

  it('does NOT dismiss on pointerdown inside the ref', () => {
    render(<Harness />);
    fireEvent.pointerDown(screen.getByText('inside'));
    expect(screen.getByTestId('box')).toHaveTextContent('open');
  });
});
