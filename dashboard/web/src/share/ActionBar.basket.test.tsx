// + Basket flow (plan §M3.5, spec §6.5 + §7.6):
//   - Click fetches /api/share/render and dispatches BASKET_ADD with
//     {panel, template_id, options, data_digest_at_add,
//     kernel_version, label_hint}.
//   - Button morphs to "✓ Added" for 800 ms then reverts.
//   - A status toast fires summarizing the add + new count.
//   - Failure path surfaces an inline action error and does NOT
//     dispatch BASKET_ADD.
import { render, screen, fireEvent, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActionBar } from './ActionBar';
import { _resetForTests, getState } from '../store/store';
import { BASKET_STORAGE_KEY } from '../store/basketSlice';
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
  localStorage.removeItem(BASKET_STORAGE_KEY);
  _resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.removeItem(BASKET_STORAGE_KEY);
});

describe('+ Basket', () => {
  it('clicking + Basket fetches the recipe and dispatches BASKET_ADD', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          body: 'md body',
          content_type: 'text/markdown',
          snapshot: {
            kernel_version: 1,
            panel: 'weekly',
            template_id: 'weekly-recap',
            options: defaults(),
            generated_at: '2026-05-11T09:00:00Z',
            data_digest: 'sha256:abc',
          },
        }),
      }),
    );
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /\+ basket/i }));
    });
    const items = getState().basket.items;
    expect(items).toHaveLength(1);
    expect(items[0].panel).toBe('weekly');
    expect(items[0].template_id).toBe('weekly-recap');
    expect(items[0].data_digest_at_add).toBe('sha256:abc');
    expect(items[0].kernel_version).toBe(1);
    expect(items[0].options).toEqual(defaults());
    expect(items[0].label_hint).toBeTruthy();
  });

  it('renders "✓ Added" for 800 ms then reverts to "+ Basket"', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          body: 'md',
          content_type: 'text/markdown',
          snapshot: {
            kernel_version: 1,
            panel: 'weekly',
            template_id: 'weekly-recap',
            options: defaults(),
            generated_at: '2026-05-11T09:00:00Z',
            data_digest: 'sha256:abc',
          },
        }),
      }),
    );
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /\+ basket/i }));
    });
    expect(screen.getByRole('button', { name: /✓ added/i })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(screen.queryByRole('button', { name: /✓ added/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /\+ basket/i })).toBeInTheDocument();
    vi.useRealTimers();
  });

  it('fires a status toast announcing the add + current count', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          body: 'md',
          content_type: 'text/markdown',
          snapshot: {
            kernel_version: 1,
            panel: 'weekly',
            template_id: 'weekly-recap',
            options: defaults(),
            generated_at: '2026-05-11T09:00:00Z',
            data_digest: 'sha256:abc',
          },
        }),
      }),
    );
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /\+ basket/i }));
    });
    const toast = getState().toast;
    expect(toast?.kind).toBe('status');
    if (toast?.kind === 'status') {
      expect(toast.text).toMatch(/added .* to basket \(1\)/i);
    }
  });

  it('does not flash "Added" or overwrite the capacity toast when the basket is full', async () => {
    // Codex P2 on PR #35 — ActionBar previously called setBasketAdded
    // and dispatched a SHOW_STATUS_TOAST `Added … to basket (N)` even
    // when BASKET_ADD bounced off the 20-section cap. That overwrote
    // the store's "Basket is full" toast with a lie about success.
    // Seed 20 items to reach the hard cap, then click + Basket and
    // assert: button does NOT morph to ✓ Added, and the toast remains
    // the capacity-rejection text (not "Added").
    const { BASKET_HARD_CAP } = await import('../store/basketSlice');
    const fullBasket = Array.from({ length: BASKET_HARD_CAP }, (_, i) => ({
      id: `seed-${i}`,
      panel: 'weekly' as const,
      template_id: 'weekly-recap',
      options: defaults(),
      added_at: 't',
      data_digest_at_add: 'sha256:seed',
      kernel_version: 1,
      label_hint: `Seed ${i}`,
    }));
    localStorage.setItem(BASKET_STORAGE_KEY, JSON.stringify(fullBasket));
    _resetForTests();  // re-hydrate from localStorage
    expect(getState().basket.items).toHaveLength(BASKET_HARD_CAP);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          body: 'md',
          content_type: 'text/markdown',
          snapshot: {
            kernel_version: 1, panel: 'weekly', template_id: 'weekly-recap',
            options: defaults(), generated_at: '2026-05-11T09:00:00Z',
            data_digest: 'sha256:new',
          },
        }),
      }),
    );
    render(
      <ActionBar
        panel="weekly" templateId="weekly-recap"
        options={defaults()} onOptionsChange={() => {}}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /\+ basket/i }));
    });
    // Button must NOT morph to ✓ Added.
    expect(screen.queryByRole('button', { name: /✓ added/i })).not.toBeInTheDocument();
    // Basket still at the cap (no growth).
    expect(getState().basket.items).toHaveLength(BASKET_HARD_CAP);
    // Toast carries the capacity-rejection text, not a misleading
    // "Added …" success message.
    const toast = getState().toast;
    expect(toast?.kind).toBe('status');
    if (toast?.kind === 'status') {
      expect(toast.text).toMatch(/basket is full/i);
      expect(toast.text).not.toMatch(/added/i);
    }
  });

  it('does NOT dispatch BASKET_ADD when the render call fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({ error: 'boom' }),
      }),
    );
    render(
      <ActionBar
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /\+ basket/i }));
    });
    expect(getState().basket.items).toHaveLength(0);
    expect(screen.getByRole('alert').textContent).toMatch(/add to basket failed/i);
  });

  it('is disabled when no template is selected', () => {
    render(
      <ActionBar
        panel="weekly"
        templateId={null}
        options={defaults()}
        onOptionsChange={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /\+ basket/i })).toBeDisabled();
  });
});
