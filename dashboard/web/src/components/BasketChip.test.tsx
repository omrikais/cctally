// BasketChip — spec §7.5 affordance contract:
//   - DOM-removed when count = 0 (NOT aria-hidden).
//   - Renders count badge when items > 0.
//   - Click dispatches OPEN_COMPOSER.
//   - aria-label includes the count for screen readers.
import { act, render, screen, fireEvent } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { BasketChip } from './BasketChip';
import { _resetForTests, dispatch, getState } from '../store/store';
import { BASKET_STORAGE_KEY, makeBasketItem } from '../store/basketSlice';
import type { ShareOptions } from '../share/types';

function defaults(): ShareOptions {
  return {
    format: 'html',
    theme: 'light',
    reveal_projects: true,
    no_branding: false,
    top_n: 5,
    period: { kind: 'current' },
    project_allowlist: null,
    show_chart: true,
    show_table: true,
  };
}

function seed(id: string) {
  return makeBasketItem({
    id,
    panel: 'weekly',
    template_id: 'weekly-recap',
    options: defaults(),
    added_at: '2026-05-11T09:00:00Z',
    data_digest_at_add: 'sha256:abc',
    kernel_version: 1,
    label_hint: 'Weekly recap',
  });
}

beforeEach(() => {
  // localStorage persists across tests in the same worker. Reset
  // BEFORE rebuilding the store so `loadInitial()` doesn't re-hydrate
  // items written by an earlier test's BASKET_ADD.
  localStorage.removeItem(BASKET_STORAGE_KEY);
  _resetForTests();
});

describe('<BasketChip>', () => {
  it('renders nothing when basket is empty (DOM removal)', () => {
    const { container } = render(<BasketChip />);
    expect(container.firstChild).toBeNull();
  });

  it('renders count badge when basket has items', () => {
    dispatch({
      type: 'BASKET_HYDRATE',
      items: [seed('a'), seed('b')],
    });
    render(<BasketChip />);
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByRole('button')).toHaveAttribute(
      'aria-label',
      expect.stringMatching(/2 sections/i),
    );
  });

  it('uses singular "section" in aria-label when count = 1', () => {
    dispatch({ type: 'BASKET_HYDRATE', items: [seed('only')] });
    render(<BasketChip />);
    expect(screen.getByRole('button')).toHaveAttribute(
      'aria-label',
      expect.stringMatching(/1 section\b/i),
    );
  });

  it('clicking dispatches OPEN_COMPOSER', () => {
    dispatch({ type: 'BASKET_HYDRATE', items: [seed('only')] });
    render(<BasketChip />);
    fireEvent.click(screen.getByRole('button'));
    expect(getState().composerModal).not.toBeNull();
  });

  it('re-renders when items grow live (useSyncExternalStore subscription)', () => {
    const { container } = render(<BasketChip />);
    expect(container.firstChild).toBeNull();
    act(() => {
      dispatch({ type: 'BASKET_ADD', item: seed('a') });
    });
    expect(screen.getByText('1')).toBeInTheDocument();
    act(() => {
      dispatch({ type: 'BASKET_ADD', item: seed('b') });
    });
    expect(screen.getByText('2')).toBeInTheDocument();
  });
});
