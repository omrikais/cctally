import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ShareIcon } from './ShareIcon';

describe('<ShareIcon>', () => {
  it('renders with accessible label', () => {
    render(<ShareIcon panel="weekly" panelLabel="Weekly" onClick={() => {}} />);
    expect(screen.getByRole('button', { name: /share weekly report/i })).toBeInTheDocument();
  });

  it('calls onClick when clicked', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<ShareIcon panel="weekly" panelLabel="Weekly" onClick={onClick} />);
    await user.click(screen.getByRole('button'));
    expect(onClick).toHaveBeenCalledOnce();
  });

  // The component lives inside panel `<section onClick={open panel modal}>`
  // elements (e.g. WeeklyPanel.tsx). A regression that drops the internal
  // stopPropagation would double-fire (open share modal + open panel
  // modal). Lock the behavior down.
  it('stops click propagation to ancestor handlers', async () => {
    const user = userEvent.setup();
    const parent = vi.fn();
    const onClick = vi.fn();
    render(
      <div onClick={parent}>
        <ShareIcon panel="weekly" panelLabel="Weekly" onClick={onClick} />
      </div>,
    );
    await user.click(screen.getByRole('button'));
    expect(onClick).toHaveBeenCalledOnce();
    expect(parent).not.toHaveBeenCalled();
  });

  // triggerId is the bridge to ShareModalRoot's focus-restore path
  // (spec §12.8). Callers pass the SAME string to both the prop here
  // and the 2nd arg of `dispatch(openShareModal(panel, triggerId))` so
  // `document.getElementById(triggerId)` resolves the trigger button.
  it('renders the optional triggerId prop as the button id', () => {
    render(
      <ShareIcon
        panel="weekly"
        panelLabel="Weekly"
        onClick={() => {}}
        triggerId="weekly-panel"
      />,
    );
    const btn = screen.getByRole('button', { name: /share weekly report/i });
    expect(btn.id).toBe('weekly-panel');
  });

  it('omits the id attribute when triggerId is not provided', () => {
    render(<ShareIcon panel="weekly" panelLabel="Weekly" onClick={() => {}} />);
    const btn = screen.getByRole('button', { name: /share weekly report/i });
    expect(btn.hasAttribute('id')).toBe(false);
  });
});
