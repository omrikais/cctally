import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ExpandButton } from './ExpandButton';

describe('ExpandButton (#264 S1)', () => {
  it('renders an accessible open button and calls onOpen', () => {
    const onOpen = vi.fn();
    render(<ExpandButton label="Forecast" onOpen={onOpen} />);
    const btn = screen.getByRole('button', { name: 'Open Forecast' });
    expect(btn).toHaveClass('panel-expand');
    fireEvent.click(btn);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('stops click propagation to the panel root', () => {
    const onOpen = vi.fn();
    const parent = vi.fn();
    render(
      <div onClick={parent}>
        <ExpandButton label="Blocks" onOpen={onOpen} />
      </div>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Open Blocks' }));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(parent).not.toHaveBeenCalled();
  });

  it('when disabled, renders a disabled button and never calls onOpen (#265 D)', () => {
    const onOpen = vi.fn();
    render(<ExpandButton label="Blocks" onOpen={onOpen} disabled />);
    const btn = screen.getByRole('button', { name: 'Blocks: nothing to open yet' });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onOpen).not.toHaveBeenCalled();
  });
});
