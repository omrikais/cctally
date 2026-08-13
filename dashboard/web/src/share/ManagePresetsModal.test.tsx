// ManagePresetsModal — plan §M2.4 contract:
//   - open=false renders nothing.
//   - open=true fetches /api/share/presets and renders a row per preset.
//   - Delete button fires DELETE then removes the row.
//   - Rename uses save-then-delete; success updates the row name.
//   - Empty-state copy renders when no presets exist.
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ManagePresetsModal } from './ManagePresetsModal';
import { _resetForTests } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
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

/**
 * Records the target's `disabled` state at the MOMENT `focus()` is called.
 *
 * #503 S3 §2 focus restore. Row actions render `disabled={busy}`, and a
 * disabled button absorbs `focus()`: the call runs, nothing throws, and
 * `document.activeElement` does not move. Asserting where focus ended up is
 * the weaker check, because that answer depends on whether React committed
 * the re-enable before the restore ran — and it did in jsdom but not in
 * Chrome for the overwrite-rename site, so that site's `activeElement`
 * assertion passed against the bug. These suites assert the call instead.
 */
function installFocusSpy(): Array<{ el: HTMLElement; disabled: boolean }> {
  const calls: Array<{ el: HTMLElement; disabled: boolean }> = [];
  const real = HTMLElement.prototype.focus;
  vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(
    function focusRecorder(this: HTMLElement, options?: FocusOptions) {
      calls.push({
        el: this,
        disabled: (this as Partial<HTMLButtonElement>).disabled === true,
      });
      real.call(this, options);
    },
  );
  return calls;
}

function focusedWhileDisabled(
  calls: Array<{ el: HTMLElement; disabled: boolean }>, el: HTMLElement | null,
): boolean[] {
  return calls.filter((c) => c.el === el).map((c) => c.disabled);
}

beforeEach(() => {
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
  _resetKeymap();
  vi.restoreAllMocks();
});

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

  it('formats Saved at through the dashboard datetime chokepoint', async () => {
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
        },
      }), { status: 200 }),
    );
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    const rendered = await screen.findByText('May 11 09:00 UTC');
    expect(rendered.tagName).toBe('TIME');
    expect(rendered).toHaveAttribute('dateTime', '2026-05-11T09:00:00Z');
    expect(screen.queryByText('2026-05-11T09:00:00Z')).not.toBeInTheDocument();
  });

  it('keeps the header outside the internally scrolling preset body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        presets: {
          weekly: {
            alpha: {
              template_id: 'weekly-recap', options: defaults(),
              saved_at: '2026-05-11T09:00:00Z',
            },
          },
        },
      }), { status: 200 }),
    );
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('alpha');
    const dialog = screen.getByRole('dialog');
    const body = dialog.querySelector('.share-manage-content');
    expect(body).not.toBeNull();
    expect(body?.querySelector('.share-manage-table')).not.toBeNull();
    expect(dialog.querySelector('.share-manage-header')?.parentElement).toBe(dialog);
    expect(body?.querySelector('.share-manage-header')).toBeNull();
  });

  it('offers explicit Save and Cancel controls while renaming', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(((
      _url: string, init?: RequestInit,
    ) => {
      if ((init?.method ?? 'GET') === 'GET') {
        return Promise.resolve(new Response(JSON.stringify({
          presets: {
            weekly: {
              alpha: {
                template_id: 'weekly-recap', options: defaults(),
                saved_at: '2026-05-11T09:00:00Z',
              },
            },
          },
        }), { status: 200 }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        panel: 'weekly', name: 'beta', template_id: 'weekly-recap',
        options: defaults(), saved_at: '2026-05-11T09:00:00Z',
      }), { status: 200 }));
    }) as typeof fetch);
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('alpha');
    fireEvent.click(screen.getByRole('button', { name: /^rename$/i }));
    const row = screen.getByDisplayValue('alpha').closest('tr');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByRole('button', { name: /^save$/i }))
      .toBeInTheDocument();
    expect(within(row as HTMLElement).getByRole('button', { name: /^cancel$/i }))
      .toBeInTheDocument();
    expect(within(row as HTMLElement).queryByRole('button', { name: /^delete$/i }))
      .not.toBeInTheDocument();
    fireEvent.change(screen.getByDisplayValue('alpha'), { target: { value: 'beta' } });
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(screen.getByText('beta')).toBeInTheDocument());
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('replaces the row actions with the armed delete confirmation', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        presets: {
          weekly: {
            alpha: {
              template_id: 'weekly-recap', options: defaults(),
              saved_at: '2026-05-11T09:00:00Z',
            },
          },
        },
      }), { status: 200 }),
    );
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('alpha');
    const row = screen.getByText('alpha').closest('tr');
    fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /^delete$/i }));
    await screen.findByText('Delete "alpha"?');
    expect(within(row as HTMLElement).queryByRole('button', { name: /^rename$/i }))
      .not.toBeInTheDocument();
    expect(within(row as HTMLElement).getAllByRole('button', { name: /^delete$/i }))
      .toHaveLength(1);
    expect(within(row as HTMLElement).getAllByRole('button', { name: /^cancel$/i }))
      .toHaveLength(1);
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
    // #503 S3 §2 — deleting destroys a config.json record with no undo, so
    // the first click only arms the confirmation.
    fireEvent.click(screen.getAllByRole('button', { name: /^delete$/i })[0]);
    expect(await screen.findByText('Delete "gone-soon"?')).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1);   // the initial GET only
    const confirmBtn = screen.getAllByRole('button', { name: /^delete$/i })
      .find((b) => b.classList.contains('share-confirm-yes'));
    fireEvent.click(confirmBtn as HTMLElement);
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

  it('Delete: focus moves to the next row action, ENABLED when it is focused',
    async () => {
      // #503 S3 §2 focus restore. The row actions render `disabled={busy}`,
      // so the next row's Rename button is disabled while the DELETE is in
      // flight. `confirm.close()` used to restore focus before the busy flag
      // was cleared, which handed the restore a disabled button: the browser
      // swallowed the call and focus stayed on <body>.
      const calls = installFocusSpy();
      let callCount = 0;
      vi.spyOn(globalThis, 'fetch').mockImplementation(((..._args: unknown[]) => {
        callCount += 1;
        if (callCount === 1) {
          const rec = {
            template_id: 'weekly-recap', options: defaults(),
            saved_at: '2026-05-11T09:00:00Z',
          };
          return Promise.resolve(new Response(JSON.stringify({
            presets: { weekly: { aaa: rec, bbb: rec } },
          }), { status: 200 }));
        }
        return Promise.resolve(new Response(null, { status: 204 }));
      }) as typeof fetch);

      render(<ManagePresetsModal open={true} onClose={() => {}} />);
      await screen.findByText('aaa');
      const nextAction = document.querySelector<HTMLButtonElement>(
        'tr[data-preset-key="weekly/bbb"] .share-manage-actions button',
      );
      expect(nextAction).not.toBeNull();

      fireEvent.click(screen.getAllByRole('button', { name: /^delete$/i })[0]);
      await screen.findByText('Delete "aaa"?');
      const confirmBtn = screen.getAllByRole('button', { name: /^delete$/i })
        .find((b) => b.classList.contains('share-confirm-yes'));
      fireEvent.click(confirmBtn as HTMLElement);

      await waitFor(() =>
        expect(screen.queryByText('aaa')).not.toBeInTheDocument());
      await waitFor(() => expect(document.activeElement).toBe(nextAction));
      expect(nextAction?.disabled).toBe(false);
      // THE assertion: the control was ENABLED when it was focused. The
      // outcome assertion above can pass for the wrong reason on a
      // different commit schedule, as it did for the overwrite-rename site.
      expect(focusedWhileDisabled(calls, nextAction)).toEqual([false]);
    });

  it('Delete: Escape cancels and nothing is deleted', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        presets: {
          weekly: {
            keeper: {
              template_id: 'weekly-recap',
              options: defaults(),
              saved_at: '2026-05-11T09:00:00Z',
            },
          },
        },
      }), { status: 200 }),
    );
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('keeper');
    fireEvent.click(screen.getAllByRole('button', { name: /^delete$/i })[0]);
    await screen.findByText('Delete "keeper"?');
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByText('Delete "keeper"?')).not.toBeInTheDocument());
    expect(screen.getByText('keeper')).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('Rename onto an existing name confirms, then replaces', async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    let renameAttempts = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation(((
      url: string, init?: RequestInit,
    ) => {
      const method = init?.method ?? 'GET';
      calls.push({ url, method, body: init?.body as string | undefined });
      if (method === 'GET') {
        return Promise.resolve(new Response(JSON.stringify({
          presets: {
            weekly: {
              mover: {
                template_id: 'weekly-recap', options: defaults(),
                saved_at: '2026-05-11T09:00:00Z', source: 'codex',
              },
              target: {
                template_id: 'weekly-recap', options: defaults(),
                saved_at: '2026-05-10T09:00:00Z', source: 'claude',
              },
            },
          },
        }), { status: 200 }));
      }
      renameAttempts += 1;
      if (renameAttempts === 1) {
        // The server decides the collision under its writer lock.
        return Promise.resolve(new Response(JSON.stringify({
          code: 'preset_name_conflict',
          error: "a preset named 'target' already exists",
          field: 'to_name',
        }), { status: 409 }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        panel: 'weekly', name: 'target', template_id: 'weekly-recap',
        options: defaults(), saved_at: '2026-05-11T09:00:00Z',
        source: 'codex',
      }), { status: 200 }));
    }) as typeof fetch);

    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('mover');
    fireEvent.click(screen.getAllByRole('button', { name: /^rename$/i })[0]);
    const input = screen.getByDisplayValue('mover');
    fireEvent.change(input, { target: { value: 'target' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    // The collision is a question, not a failure banner.
    expect(
      await screen.findByText('A preset named "target" already exists'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Replace it' }));
    await waitFor(() => expect(renameAttempts).toBe(2));
    const second = calls.filter((c) => c.method === 'POST')[1];
    expect(JSON.parse(second.body as string)).toEqual({
      panel: 'weekly', from_name: 'mover', to_name: 'target',
      overwrite: true,
    });
    await waitFor(() =>
      expect(screen.queryByText('mover')).not.toBeInTheDocument());
  });

  it('Rename collision: focus skips the row the overwrite destroys', async () => {
    // #503 S3 §2 focus fallback. An overwrite-rename destroys TWO rows —
    // the one being renamed and the one holding the target name — and the
    // second can be the very next row, which is what the plain "next row's
    // first action" answer resolves to. `restore()` declines a detached
    // node, so focus then landed on <body>.
    //
    // The surviving row's action is ALSO disabled while the rename is in
    // flight, so this pins the second half of the same requirement: the
    // control was enabled at the moment it was focused. The `activeElement`
    // assertion below passed against that defect — jsdom committed the
    // re-enable before the restore ran and Chrome did not — which is why
    // the call-time assertion is the one that states the requirement.
    const calls = installFocusSpy();
    let renameAttempts = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation(((
      _url: string, init?: RequestInit,
    ) => {
      const method = init?.method ?? 'GET';
      if (method === 'GET') {
        const rec = {
          template_id: 'weekly-recap', options: defaults(),
          saved_at: '2026-05-11T09:00:00Z',
        };
        // Sorted by name, so the DOM order is aaa, bbb, ccc — and bbb, the
        // collision target, sits immediately after the row being renamed.
        return Promise.resolve(new Response(JSON.stringify({
          presets: { weekly: { aaa: rec, bbb: rec, ccc: rec } },
        }), { status: 200 }));
      }
      renameAttempts += 1;
      if (renameAttempts === 1) {
        return Promise.resolve(new Response(JSON.stringify({
          code: 'preset_name_conflict',
          error: "a preset named 'bbb' already exists",
          field: 'to_name',
        }), { status: 409 }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        panel: 'weekly', name: 'bbb', template_id: 'weekly-recap',
        options: defaults(), saved_at: '2026-05-11T09:00:00Z',
      }), { status: 200 }));
    }) as typeof fetch);

    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('aaa');
    const survivor = document.querySelector<HTMLElement>(
      'tr[data-preset-key="weekly/ccc"] .share-manage-actions button',
    );
    expect(survivor).not.toBeNull();

    fireEvent.click(screen.getAllByRole('button', { name: /^rename$/i })[0]);
    const input = screen.getByDisplayValue('aaa');
    fireEvent.change(input, { target: { value: 'bbb' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await screen.findByText('A preset named "bbb" already exists');

    fireEvent.click(screen.getByRole('button', { name: 'Replace it' }));
    await waitFor(() => expect(renameAttempts).toBe(2));
    // Focus skipped bbb and landed on the first surviving row's action.
    await waitFor(() => expect(document.activeElement).toBe(survivor));
    expect((survivor as HTMLButtonElement).disabled).toBe(false);
    expect(focusedWhileDisabled(calls, survivor)).toEqual([false]);
  });

  it('Rename collision: cancelling replaces nothing', async () => {
    let renameAttempts = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation(((
      _url: string, init?: RequestInit,
    ) => {
      const method = init?.method ?? 'GET';
      if (method === 'GET') {
        return Promise.resolve(new Response(JSON.stringify({
          presets: {
            weekly: {
              mover: {
                template_id: 'weekly-recap', options: defaults(),
                saved_at: '2026-05-11T09:00:00Z',
              },
            },
          },
        }), { status: 200 }));
      }
      renameAttempts += 1;
      return Promise.resolve(new Response(JSON.stringify({
        code: 'preset_name_conflict', error: 'exists', field: 'to_name',
      }), { status: 409 }));
    }) as typeof fetch);

    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    await screen.findByText('mover');
    fireEvent.click(screen.getAllByRole('button', { name: /^rename$/i })[0]);
    const input = screen.getByDisplayValue('mover');
    fireEvent.change(input, { target: { value: 'target' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await screen.findByText('A preset named "target" already exists');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(
      screen.queryByText('A preset named "target" already exists'),
    ).not.toBeInTheDocument());
    expect(renameAttempts).toBe(1);
    expect(screen.getByText('mover')).toBeInTheDocument();
  });

  it('renders empty-state when no presets exist', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ presets: {} }), { status: 200 }),
    );
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    expect(await screen.findByText(/no saved presets yet/i)).toBeInTheDocument();
  });

  it('Rename: ONE request to the rename endpoint, updates the row', async () => {
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
          options: defaults(), saved_at: '2026-05-11T09:00:00Z',
          source: 'codex',
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
    // #503 S3 §1 — deliberately replaces the old ['POST','DELETE']
    // assertion. A rename is ONE atomic server operation now; the pair was
    // what dropped `source` and reset `saved_at`.
    const mutationCalls = calls.filter((c) => c.method !== 'GET');
    expect(mutationCalls.map((c) => c.method)).toEqual(['POST']);
    expect(mutationCalls[0].url).toBe('/api/share/presets/rename');
    expect(JSON.parse(mutationCalls[0].body as string)).toEqual({
      panel: 'weekly', from_name: 'old-name', to_name: 'new-name',
    });
    // The row shows what the SERVER stored, not the stale local record:
    // `saved_at` is unchanged by a rename because the recipe did not change.
    expect(screen.getByText('May 11 09:00 UTC')).toBeInTheDocument();
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
