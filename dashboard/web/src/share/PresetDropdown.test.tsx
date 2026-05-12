// PresetDropdown — plan §M2.4 contract:
//   - Trigger button toggles the menu open/closed.
//   - Menu lazy-fetches /api/share/presets on first open.
//   - Items rendered alphabetically for the current panel only.
//   - "No saved presets yet." when the panel bucket is empty.
//   - Clicking an item invokes onPick with (template_id, options).
//   - "Manage presets…" footer item invokes onManage.
//   - Click-outside closes the menu.
//
// Plan §M4.3 additions:
//   - Menu also lazy-fetches /api/share/history on first open.
//   - "Recent shares" group below the presets list, filtered to the
//     current panel, newest first.
//   - Clicking a history row invokes onPick with the recipe's
//     template_id + options.
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

// The dropdown fires two GETs on open — `/api/share/presets` and
// `/api/share/history` (M4.3 added the second). Tests mock both via a
// URL-keyed router so a tweak to either endpoint's payload doesn't
// break the other group's assertions.
//
// `historyPayload` defaults to an empty buffer so legacy presets tests
// pre-M4.3 still pass without modification.
type HistoryPayload = { history: unknown[] };
type PresetsPayload = { presets: unknown };

function mockFetchBoth(
  presets: PresetsPayload,
  history: HistoryPayload = { history: [] },
) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(
    (input: RequestInfo | URL): Promise<Response> => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.startsWith('/api/share/history')) {
        return Promise.resolve(
          new Response(JSON.stringify(history), { status: 200 }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify(presets), { status: 200 }),
      );
    },
  );
}

afterEach(() => vi.restoreAllMocks());

describe('<PresetDropdown>', () => {
  it('renders the trigger button, menu hidden initially', () => {
    mockFetchBoth({ presets: {} });
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    expect(screen.getByRole('button', { name: /presets/i })).toBeInTheDocument();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('lazy-fetches presets and history on first open', async () => {
    const fetchSpy = mockFetchBoth({
      presets: {
        weekly: {
          'team-monday': {
            template_id: 'weekly-recap',
            options: defaults(),
            saved_at: '2026-05-11T09:00:00Z',
          },
        },
      },
    });
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
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/share/history',
        expect.any(Object),
      );
    });
  });

  it('shows empty-state copy when the panel has no presets', async () => {
    mockFetchBoth({ presets: {} });
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    expect(await screen.findByText(/no saved presets yet/i)).toBeInTheDocument();
  });

  it('lists preset names alphabetically and invokes onPick on click', async () => {
    const opts = defaults();
    mockFetchBoth({
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
    });
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
    mockFetchBoth({ presets: {} });
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
    mockFetchBoth({
      presets: {
        daily: {
          'never-shown': {
            template_id: 'daily-recap',
            options: defaults(),
            saved_at: '2026-05-11T09:00:00Z',
          },
        },
      },
    });
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    expect(await screen.findByText(/no saved presets yet/i)).toBeInTheDocument();
    expect(screen.queryByText('never-shown')).not.toBeInTheDocument();
  });

  it('closes the menu when clicking outside', async () => {
    mockFetchBoth({ presets: {} });
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

  // ---- M4.3 — "Recent shares" history group ----

  it('renders Recent shares group with rows filtered to current panel', async () => {
    const opts = defaults();
    mockFetchBoth(
      { presets: {} },
      {
        history: [
          {
            recipe_id: 'r-other',
            panel: 'daily',
            template_id: 'daily-recap',
            options: opts,
            format: 'md',
            destination: 'copy',
            exported_at: '2026-05-10T10:00:00Z',
          },
          {
            recipe_id: 'r-weekly-1',
            panel: 'weekly',
            template_id: 'weekly-recap',
            options: opts,
            format: 'md',
            destination: 'copy',
            exported_at: '2026-05-11T09:00:00Z',
          },
          {
            recipe_id: 'r-weekly-2',
            panel: 'weekly',
            template_id: 'weekly-visual',
            options: { ...opts, theme: 'dark' },
            format: 'svg',
            destination: 'download',
            exported_at: '2026-05-11T10:30:00Z',
          },
        ],
      },
    );
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    expect(await screen.findByText(/recent shares/i)).toBeInTheDocument();
    // Newest first — `r-weekly-2` precedes `r-weekly-1` in the DOM.
    const items = Array.from(
      document.querySelectorAll('.share-presets-history-item'),
    );
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toMatch(/weekly-visual/);
    expect(items[1].textContent).toMatch(/weekly-recap/);
    // Daily row stays hidden.
    expect(screen.queryByText(/daily-recap/)).not.toBeInTheDocument();
  });

  it('clicking a history row invokes onPick with that recipe', async () => {
    const opts = defaults();
    mockFetchBoth(
      { presets: {} },
      {
        history: [
          {
            recipe_id: 'r1',
            panel: 'weekly',
            template_id: 'weekly-recap',
            options: { ...opts, top_n: 7 },
            format: 'md',
            destination: 'copy',
            exported_at: '2026-05-11T09:00:00Z',
          },
        ],
      },
    );
    const onPick = vi.fn();
    render(
      <PresetDropdown panel="weekly" onPick={onPick} onManage={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    await screen.findByText(/recent shares/i);
    const row = document.querySelector('.share-presets-history-item');
    expect(row).not.toBeNull();
    fireEvent.click(row as Element);
    expect(onPick).toHaveBeenCalledWith('weekly-recap', { ...opts, top_n: 7 });
  });

  it('hides Recent shares group when history is empty for this panel', async () => {
    mockFetchBoth(
      { presets: {} },
      {
        history: [
          {
            recipe_id: 'r-other',
            panel: 'daily',
            template_id: 'daily-recap',
            options: defaults(),
            format: 'md',
            destination: 'copy',
            exported_at: '2026-05-11T09:00:00Z',
          },
        ],
      },
    );
    render(
      <PresetDropdown panel="weekly" onPick={() => {}} onManage={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /presets/i }));
    await screen.findByText(/no saved presets yet/i);
    expect(screen.queryByText(/recent shares/i)).not.toBeInTheDocument();
  });
});
