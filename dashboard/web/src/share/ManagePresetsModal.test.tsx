// ManagePresetsModal — plan §M2.4 contract:
//   - open=false renders nothing.
//   - open=true fetches /api/share/presets and renders a row per preset.
//   - Delete button fires DELETE then removes the row.
//   - Rename uses save-then-delete; success updates the row name.
//   - Empty-state copy renders when no presets exist.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ManagePresetsModal } from './ManagePresetsModal';
import { _resetForTests } from '../store/store';
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

beforeEach(() => {
  _resetForTests();
});

afterEach(() => vi.restoreAllMocks());

describe('<ManagePresetsModal>', () => {
  it('open=false renders nothing', () => {
    const { container } = render(
      <ManagePresetsModal open={false} onClose={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('lists presets across panels in stable sort order', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        presets: {
          weekly: {
            'team-monday': {
              template_id: 'weekly-recap',
              options: defaults(),
              saved_at: '2026-05-11T09:00:00Z',
            },
          },
          daily: {
            'morning': {
              template_id: 'daily-recap',
              options: defaults(),
              saved_at: '2026-05-11T08:00:00Z',
            },
          },
        },
      }), { status: 200 }),
    );
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('morning');
    expect(screen.getByText('morning')).toBeInTheDocument();
    expect(screen.getByText('team-monday')).toBeInTheDocument();
  });

  it('Delete button fires DELETE and removes the row', async () => {
    let callCount = 0;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(((..._args: unknown[]) => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve(new Response(JSON.stringify({
          presets: {
            weekly: {
              'gone-soon': {
                template_id: 'weekly-recap',
                options: defaults(),
                saved_at: '2026-05-11T09:00:00Z',
              },
            },
          },
        }), { status: 200 }));
      }
      // DELETE
      return Promise.resolve(new Response(null, { status: 204 }));
    }) as typeof fetch);
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('gone-soon');
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }));
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/share/presets/weekly/gone-soon',
        expect.objectContaining({ method: 'DELETE' }),
      );
    });
    await waitFor(() => {
      expect(screen.queryByText('gone-soon')).not.toBeInTheDocument();
    });
  });

  it('renders empty-state when no presets exist', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ presets: {} }), { status: 200 }),
    );
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    expect(await screen.findByText(/no saved presets yet/i)).toBeInTheDocument();
  });

  it('Rename: save under new name then delete old, updates the row', async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      calls.push({ url, method, body: init?.body as string | undefined });
      if (method === 'GET') {
        return Promise.resolve(new Response(JSON.stringify({
          presets: {
            weekly: {
              'old-name': {
                template_id: 'weekly-recap',
                options: defaults(),
                saved_at: '2026-05-11T09:00:00Z',
              },
            },
          },
        }), { status: 200 }));
      }
      if (method === 'POST') {
        return Promise.resolve(new Response(JSON.stringify({
          panel: 'weekly', name: 'new-name', template_id: 'weekly-recap',
          options: defaults(), saved_at: '2026-05-11T10:00:00Z',
        }), { status: 200 }));
      }
      // DELETE
      return Promise.resolve(new Response(null, { status: 204 }));
    }) as typeof fetch);

    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('old-name');
    fireEvent.click(screen.getByRole('button', { name: /^rename$/i }));
    const input = screen.getByDisplayValue('old-name');
    fireEvent.change(input, { target: { value: 'new-name' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(screen.queryByText('new-name')).toBeInTheDocument());
    expect(screen.queryByText('old-name')).not.toBeInTheDocument();
    // POST then DELETE — save-new-then-delete-old order.
    const mutationCalls = calls.filter((c) => c.method !== 'GET');
    expect(mutationCalls.map((c) => c.method)).toEqual(['POST', 'DELETE']);
    expect(mutationCalls[1].url).toBe('/api/share/presets/weekly/old-name');
  });

  it('close button fires onClose', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ presets: {} }), { status: 200 }),
    );
    const onClose = vi.fn();
    render(<ManagePresetsModal open={true} onClose={onClose} />);
    await screen.findByText(/no saved presets yet/i);
    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
