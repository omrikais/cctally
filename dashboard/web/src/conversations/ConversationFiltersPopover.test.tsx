import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationFiltersPopover } from './ConversationFiltersPopover';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import { clearRailPrefs } from '../store/conversationRailPrefs';
import type { Envelope } from '../types/envelope';

// Minimal display-only envelope: the popover only reads the `display` block (via
// useDisplayTz) for the date-preset tz, so the rest is irrelevant here.
function displayEnvelope(resolvedTz: string, generatedAt = '2026-07-01T03:00:00Z'): Envelope {
  return {
    envelope_version: 2,
    generated_at: generatedAt,
    display: { tz: resolvedTz, resolved_tz: resolvedTz, offset_label: resolvedTz, offset_seconds: 0 },
  } as unknown as Envelope;
}

// Stub the facets hook so the project multi-select renders from a fixture, not a
// live fetch (mirrors the ConversationRail.test.tsx hook-stub convention).
let facetProjects = [{ project_label: 'projA', count: 4 }, { project_label: 'projB', count: 1 }];
vi.mock('../hooks/useConversationFacets', () => ({
  useConversationFacets: () => ({ projects: facetProjects }),
}));

beforeEach(() => {
  // #217 S4 / I-2.2 — filters now persist; clear before reset so a prior test's
  // costMin/costMax edit can't bleed into loadInitial.
  clearRailPrefs();
  _resetForTests();
  facetProjects = [{ project_label: 'projA', count: 4 }, { project_label: 'projB', count: 1 }];
  vi.useFakeTimers();
});
afterEach(() => {
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  clearRailPrefs();
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

  it('computes the date-preset month in display.tz, not raw UTC', () => {
    // FINDING 4: at UTC 2026-07-01T03:00Z a user in America/Los_Angeles (UTC-7
    // in July) is still on the wall-clock day 2026-06-30. A raw-UTC preset would
    // pick JULY; the display-tz preset must pick JUNE so it matches the server's
    // display-tz interpretation of the YYYY-MM-DD bounds.
    vi.setSystemTime(new Date('2026-07-01T03:00:00Z'));
    updateSnapshot(displayEnvelope('America/Los_Angeles'));
    render(<ConversationFiltersPopover />);
    fireEvent.click(screen.getByRole('button', { name: /this month/i }));
    const f = getState().conversationFilters;
    // June, not July.
    expect(f.dateFrom).toBe('2026-06-01');
    expect(f.dateTo).toBe('2026-06-30');
  });

  it('uses the UTC month when the resolved tz is Etc/UTC', () => {
    // Same instant, default UTC tz → the preset IS July (the UTC wall clock has
    // already rolled over). Proves the test above is non-vacuous (the tz, not a
    // constant, drives the month).
    vi.setSystemTime(new Date('2026-07-01T03:00:00Z'));
    updateSnapshot(displayEnvelope('Etc/UTC'));
    render(<ConversationFiltersPopover />);
    fireEvent.click(screen.getByRole('button', { name: /this month/i }));
    const f = getState().conversationFilters;
    expect(f.dateFrom).toBe('2026-07-01');
    expect(f.dateTo).toBe('2026-07-31');
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

  it('keeps per-field debounce timers independent (editing Max does not drop a pending Min)', () => {
    // FINDING 1 regression: a single shared debounce timer would have the
    // costMax edit clear the costMin timer, dropping the Min dispatch — the Min
    // input still DISPLAYS '5' but never reaches the store. Each numeric field
    // must own its own timer so both dispatches survive the window.
    render(<ConversationFiltersPopover />);
    const minInput = screen.getByLabelText(/min cost/i) as HTMLInputElement;
    const maxInput = screen.getByLabelText(/max cost/i) as HTMLInputElement;
    // Edit Min, then Max WITHIN the 300ms window (well under it).
    fireEvent.change(minInput, { target: { value: '5' } });
    vi.advanceTimersByTime(100);
    fireEvent.change(maxInput, { target: { value: '10' } });
    // Neither applied yet (both still pending).
    expect(getState().conversationFilters.costMin).toBeNull();
    expect(getState().conversationFilters.costMax).toBeNull();
    // Flush past both fields' debounce.
    vi.advanceTimersByTime(350);
    // BOTH must have reached the store — the Min timer wasn't cancelled by Max.
    expect(getState().conversationFilters.costMin).toBe(5);
    expect(getState().conversationFilters.costMax).toBe(10);
    // The displayed Min value matches the applied filter (no silent display/store
    // divergence).
    expect(minInput.value).toBe('5');
  });

  it('keeps the rebuild-min timer independent of the cost timers', () => {
    // A third numeric (rebuildMin) edited within the same window as a cost edit
    // must also survive — proves the per-field keying covers all three axes.
    render(<ConversationFiltersPopover />);
    const maxInput = screen.getByLabelText(/max cost/i) as HTMLInputElement;
    const rebuildInput = screen.getByLabelText(/min cache rebuilds/i) as HTMLInputElement;
    fireEvent.change(maxInput, { target: { value: '8' } });
    vi.advanceTimersByTime(50);
    fireEvent.change(rebuildInput, { target: { value: '3' } });
    vi.advanceTimersByTime(350);
    expect(getState().conversationFilters.costMax).toBe(8);
    expect(getState().conversationFilters.rebuildMin).toBe(3);
  });

  it('sets inputMode on a numeric input focus and clears it on blur', () => {
    render(<ConversationFiltersPopover />);
    const input = screen.getByLabelText(/min cost/i);
    fireEvent.focus(input);
    expect(getState().inputMode).toBe('filter');
    fireEvent.blur(input);
    expect(getState().inputMode).toBeNull();
  });

  it('resets inputMode on unmount (focused without a preceding blur)', () => {
    // FINDING 3: the popover unmounts conditionally (convFiltersOpen && !isSearching).
    // If it unmounts while a numeric input holds focus without firing blur,
    // inputMode would stay 'filter' and suppress reader hotkeys until the next
    // focus/blur. A defensive unmount cleanup must reset it to null.
    const { unmount } = render(<ConversationFiltersPopover />);
    const input = screen.getByLabelText(/min cost/i);
    fireEvent.focus(input);
    expect(getState().inputMode).toBe('filter');
    // Unmount WITHOUT a blur (simulates the conditional unmount stealing focus).
    unmount();
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
