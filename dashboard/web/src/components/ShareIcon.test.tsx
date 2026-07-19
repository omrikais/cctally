import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ShareIcon } from './ShareIcon';
import { _resetForTests, dispatch } from '../store/store';

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
  // elements (e.g. BlocksPanel.tsx). A regression that drops the internal
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

  // Issue #67 — `dataTestId` is forwarded to the rendered <button> as
  // `data-testid`, so callers nested inside an enclosing section's
  // `onClick` (e.g. ProjectsModal) can `screen.getByTestId(...)` the
  // button directly instead of wrapping ShareIcon in a sentinel <span>.
  it('renders the optional dataTestId prop as the button data-testid', () => {
    render(
      <ShareIcon
        panel="weekly"
        panelLabel="Weekly"
        onClick={() => {}}
        dataTestId="share-icon-weekly-panel"
      />,
    );
    const btn = screen.getByTestId('share-icon-weekly-panel');
    expect(btn.tagName).toBe('BUTTON');
    expect(btn.getAttribute('data-share-panel')).toBe('weekly');
  });

  it('omits the data-testid attribute when dataTestId is not provided', () => {
    render(<ShareIcon panel="weekly" panelLabel="Weekly" onClick={() => {}} />);
    const btn = screen.getByRole('button', { name: /share weekly report/i });
    expect(btn.hasAttribute('data-testid')).toBe(false);
  });
});

// #294 S5 §7 — the per-source share matrix gates the ShareIcon render itself
// (the click-path chokepoint), so a disallowed (source, panel) pair exposes NO
// share affordance at all — not merely an inert button the server later
// rejects. Every panel/modal share entry point routes through <ShareIcon>, so
// gating here covers all of them at once.
describe('<ShareIcon> source-matrix gating (§7)', () => {
  // The default store source is claude; reset after each so the plain suite
  // above (which never touches the store) keeps rendering under claude.
  afterEach(() => {
    _resetForTests();
  });

  it('renders forecast for `all`', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    render(<ShareIcon panel="forecast" panelLabel="Forecast" onClick={() => {}} />);
    expect(screen.getByRole('button', { name: /share forecast report/i })).toBeInTheDocument();
  });

  it('renders trend for `all`', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    render(<ShareIcon panel="trend" panelLabel="Trend" onClick={() => {}} />);
    expect(screen.getByRole('button', { name: /share trend report/i })).toBeInTheDocument();
  });

  it('renders forecast for `codex`', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    render(<ShareIcon panel="forecast" panelLabel="Forecast" onClick={() => {}} />);
    expect(screen.getByRole('button', { name: /share forecast report/i })).toBeInTheDocument();
  });

  it('KEEPS the forecast/trend affordance under `claude` (full 9-panel set)', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' }));
    for (const panel of ['forecast', 'trend'] as const) {
      const { unmount } = render(
        <ShareIcon panel={panel} panelLabel={panel} onClick={() => {}} />,
      );
      expect(
        screen.getByRole('button', { name: new RegExp(`share ${panel} report`, 'i') }),
      ).toBeInTheDocument();
      unmount();
    }
  });

  it('KEEPS the affordance for a codex-visible panel (weekly) under `codex` (7-panel set)', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    render(<ShareIcon panel="weekly" panelLabel="Weekly" onClick={() => {}} />);
    expect(
      screen.getByRole('button', { name: /share weekly report/i }),
    ).toBeInTheDocument();
  });

  it('KEEPS the hero current-week affordance under `all` (current-week is in every matrix)', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    render(<ShareIcon panel="current-week" panelLabel="Current week" onClick={() => {}} />);
    expect(
      screen.getByRole('button', { name: /share current week report/i }),
    ).toBeInTheDocument();
  });
});
