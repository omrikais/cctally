import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationFiltersPopover } from './ConversationFiltersPopover';
import { _resetForTests, dispatch, getState } from '../store/store';

// Stub the facets hook so the project multi-select renders from a fixture, not a
// live fetch (mirrors the ConversationRail.test.tsx hook-stub convention).
let facetProjects = [{ project_label: 'projA', count: 4 }, { project_label: 'projB', count: 1 }];
vi.mock('../hooks/useConversationFacets', () => ({
  useConversationFacets: () => ({ projects: facetProjects }),
}));

beforeEach(() => {
  _resetForTests();
  facetProjects = [{ project_label: 'projA', count: 4 }, { project_label: 'projB', count: 1 }];
  vi.useFakeTimers();
});
afterEach(() => {
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  _resetForTests();
  vi.restoreAllMocks();
});

describe('ConversationFiltersPopover', () => {
  it('renders the four filter sections', () => {
    render(<ConversationFiltersPopover />);
    // Date presets
    expect(screen.getByRole('button', { name: /this month/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /last month/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /last 7d/i })).toBeTruthy();
    // Project options from the facets fixture (label shows the count).
    expect(screen.getByText(/projA/)).toBeTruthy();
    expect(screen.getByText(/projB/)).toBeTruthy();
    // Cost presets + rebuild presets + footer.
    expect(screen.getByRole('button', { name: '≥$1' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Clear all' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy();
  });

  it('a date preset chip dispatches concrete from/to + a preset label', () => {
    render(<ConversationFiltersPopover />);
    fireEvent.click(screen.getByRole('button', { name: /this month/i }));
    const f = getState().conversationFilters;
    expect(f.datePreset).toBe('this-month');
    // YYYY-MM-DD bounds, from <= to, same calendar month.
    expect(f.dateFrom).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(f.dateTo).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(f.dateFrom!.slice(0, 7)).toBe(f.dateTo!.slice(0, 7));
    expect(f.dateFrom! <= f.dateTo!).toBe(true);
    expect(f.dateFrom!.endsWith('-01')).toBe(true);
  });

  it('the Last 7d preset spans 7 days ending today', () => {
    render(<ConversationFiltersPopover />);
    fireEvent.click(screen.getByRole('button', { name: /last 7d/i }));
    const f = getState().conversationFilters;
    expect(f.datePreset).toBe('last-7d');
    expect(f.dateFrom! < f.dateTo!).toBe(true);
  });

  it('toggling a project checkbox updates the projects axis', () => {
    render(<ConversationFiltersPopover />);
    const cb = screen.getByRole('checkbox', { name: /projA/ });
    fireEvent.click(cb);
    expect(getState().conversationFilters.projects).toContain('projA');
    fireEvent.click(cb);
    expect(getState().conversationFilters.projects).not.toContain('projA');
  });

  it('a cost preset chip sets costMin', () => {
    render(<ConversationFiltersPopover />);
    fireEvent.click(screen.getByRole('button', { name: '≥$5' }));
    expect(getState().conversationFilters.costMin).toBe(5);
  });

  it('a rebuild preset chip sets rebuildMin', () => {
    render(<ConversationFiltersPopover />);
    fireEvent.click(screen.getByRole('button', { name: '≥3' }));
    expect(getState().conversationFilters.rebuildMin).toBe(3);
  });

  it('debounces the cost-min numeric input (~300ms)', () => {
    render(<ConversationFiltersPopover />);
    const input = screen.getByLabelText(/min cost/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: '7' } });
    // Not applied immediately.
    expect(getState().conversationFilters.costMin).toBeNull();
    vi.advanceTimersByTime(350);
    expect(getState().conversationFilters.costMin).toBe(7);
  });

  it('sets inputMode on a numeric input focus and clears it on blur', () => {
    render(<ConversationFiltersPopover />);
    const input = screen.getByLabelText(/min cost/i);
    fireEvent.focus(input);
    expect(getState().inputMode).toBe('filter');
    fireEvent.blur(input);
    expect(getState().inputMode).toBeNull();
  });

  it('Clear all dispatches CLEAR_CONVERSATION_FILTERS', () => {
    dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 3, projects: ['projA'] } });
    render(<ConversationFiltersPopover />);
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    expect(getState().conversationFilters.projects).toEqual([]);
    expect(getState().conversationFilters.rebuildMin).toBeNull();
  });

  it('Done closes the popover', () => {
    dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: true });
    render(<ConversationFiltersPopover />);
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(getState().convFiltersOpen).toBe(false);
  });

  it('reflects an active project selection as a checked checkbox', () => {
    dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { projects: ['projB'] } });
    render(<ConversationFiltersPopover />);
    const cb = screen.getByRole('checkbox', { name: /projB/ }) as HTMLInputElement;
    expect(cb.checked).toBe(true);
    // projA, unselected, stays unchecked.
    expect((screen.getByRole('checkbox', { name: /projA/ }) as HTMLInputElement).checked).toBe(false);
  });

  it('shows the project count next to each option', () => {
    render(<ConversationFiltersPopover />);
    const projA = screen.getByRole('checkbox', { name: /projA/ }).closest('label')!;
    expect(within(projA).getByText('4')).toBeTruthy();
  });
});
