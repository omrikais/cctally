// PresetDropdown — plan §M2.4 contract:
//   - Trigger button toggles the menu open/closed.
//   - Menu lazy-fetches /api/share/presets on first open.
//   - Items rendered alphabetically for the current panel only.
//   - "No saved presets yet." when the panel bucket is empty.
//   - Clicking an item invokes onPick with (template_id, options).
//   - "Manage presets…" footer item invokes onManage.
//   - Click-outside closes the menu.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PresetDropdown } from './PresetDropdown';
import type { ShareOptions } from './types';

function defaults(): ShareOptions {
  return {
    format: 'md',
    theme: 'light',
    reveal_projects: false,
    no_branding: false,
    top_n: 5,
    period: { kind: 'current' },
    project_allowlist: null,
    show_chart: true,
    show_table: true,
  };
}

afterEach(() => vi.restoreAllMocks());

describe('<PresetDropdown>', () => {
  it('renders the trigger button, menu hidden initially', () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ presets: {} }), { status: 200 }),
    );
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    expect(screen.getByRole('button', { name: /presets/i })).toBeInTheDocument();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('lazy-fetches presets on first open', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        presets: {
          weekly: {
            'team-monday': {
              template_id: 'weekly-recap',
              options: defaults(),
              saved_at: '2026-05-11T09:00:00Z',
            },
          },
        },
      }), { status: 200 }),
    );
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    // Before open, no fetch.
    expect(fetchSpy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/share/presets',
        expect.any(Object),
      );
    });
  });

  it('shows empty-state copy when the panel has no presets', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ presets: {} }), { status: 200 }),
    );
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    expect(await screen.findByText(/no saved presets yet/i)).toBeInTheDocument();
  });

  it('lists preset names alphabetically and invokes onPick on click', async () => {
    const opts = defaults();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        presets: {
          weekly: {
            'zebra': {
              template_id: 'weekly-visual',
              options: { ...opts, theme: 'dark' },
              saved_at: '2026-05-11T09:00:00Z',
            },
            'alpha': {
              template_id: 'weekly-recap',
              options: opts,
              saved_at: '2026-05-11T08:00:00Z',
            },
          },
        },
      }), { status: 200 }),
    );
    const onPick = vi.fn();
    render(
      <PresetDropdown panel="weekly" onPick={onPick} onManage={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    // Wait for "alpha" to appear, then collect the preset-row items.
    await screen.findByRole('menuitem', { name: 'alpha' });
    const alpha = screen.getByRole('menuitem', { name: 'alpha' });
    const zebra = screen.getByRole('menuitem', { name: 'zebra' });
    // DOM order reflects the sorted output.
    const order = Array.from(
      document.querySelectorAll('.share-presets-item'),
    ).map((el) => el.textContent);
    expect(order).toEqual(['alpha', 'zebra']);
    fireEvent.click(alpha);
    expect(onPick).toHaveBeenCalledWith('weekly-recap', opts);
    void zebra;  // referenced for the type-check; assertion above covers order
  });

  it('invokes onManage when "Manage presets…" is clicked', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ presets: {} }), { status: 200 }),
    );
    const onManage = vi.fn();
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={onManage} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    const manage = await screen.findByRole('menuitem', { name: /manage presets/i });
    fireEvent.click(manage);
    expect(onManage).toHaveBeenCalledTimes(1);
  });

  it('filters by panel — does not surface other panels\' presets', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        presets: {
          daily: {
            'never-shown': {
              template_id: 'daily-recap',
              options: defaults(),
              saved_at: '2026-05-11T09:00:00Z',
            },
          },
        },
      }), { status: 200 }),
    );
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    expect(await screen.findByText(/no saved presets yet/i)).toBeInTheDocument();
    expect(screen.queryByText('never-shown')).not.toBeInTheDocument();
  });

  it('closes the menu when clicking outside', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ presets: {} }), { status: 200 }),
    );
    render(
      <div>
        <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />
        <div data-testid="outside">outside</div>
      </div>,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    await screen.findByRole('menu');
    fireEvent.mouseDown(screen.getByTestId('outside'));
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument());
  });
});
