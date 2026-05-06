import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SessionsPanel } from '../src/panels/SessionsPanel';
import { updateSnapshot, _resetForTests, getState, dispatch } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<SessionsPanel />', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the sessions table with rows', () => {
    render(<SessionsPanel />);
    const rows = document.querySelectorAll('.session-row');
    expect(rows.length).toBeGreaterThan(0);
  });

  it('renders the panel-header with clock icon and "(N total)" sub-span', () => {
    render(<SessionsPanel />);
    const header = document.querySelector('#panel-sessions .panel-header');
    expect(header).not.toBeNull();
    const useEl = header?.querySelector('svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#clock');
    const sub = header?.querySelector('h3 .sub');
    expect(sub?.textContent).toMatch(/20 total/);
  });

  it('renders the sessions-ctrls with funnel, magnifier, and sort-updown icons', () => {
    render(<SessionsPanel />);
    const ctrls = document.getElementById('sessions-ctrls');
    expect(ctrls).not.toBeNull();
    const uses = ctrls?.querySelectorAll('svg use');
    const hrefs = Array.from(uses ?? []).map((u) => u.getAttribute('href'));
    expect(hrefs).toContain('/static/icons.svg#funnel');
    expect(hrefs).toContain('/static/icons.svg#magnifier');
    expect(hrefs).toContain('/static/icons.svg#sort-updown');
  });

  it('sort-pill shows "sort: started desc" by default and cycles on click', async () => {
    render(<SessionsPanel />);
    const pill = document.getElementById('sort-pill');
    expect(pill).not.toBeNull();
    const label = pill?.querySelector('.label');
    expect(label?.textContent).toBe('sort: started desc');
    const user = userEvent.setup();
    await user.click(pill!);
    expect(getState().sessionsSort).toBe('cost desc');
    await user.click(pill!);
    expect(getState().sessionsSort).toBe('duration desc');
    await user.click(pill!);
    expect(getState().sessionsSort).toBe('model asc');
    await user.click(pill!);
    expect(getState().sessionsSort).toBe('project asc');
    // wraps back to started desc
    await user.click(pill!);
    expect(getState().sessionsSort).toBe('started desc');
  });

  it('model chips derive the opus/haiku/sonnet class from the model name', () => {
    render(<SessionsPanel />);
    // Fixture has 3 repeating families — opus/sonnet/haiku. Ensure each class appears.
    expect(document.querySelectorAll('.model-chip.opus').length).toBeGreaterThan(0);
    expect(document.querySelectorAll('.model-chip.sonnet').length).toBeGreaterThan(0);
    expect(document.querySelectorAll('.model-chip.haiku').length).toBeGreaterThan(0);
  });

  it('cost cell carries cost-xs/low/mid/high class per legacy thresholds', () => {
    render(<SessionsPanel />);
    const numCells = document.querySelectorAll('#sess-rows td.num');
    const classes = new Set<string>();
    numCells.forEach((c) => {
      ['cost-xs', 'cost-low', 'cost-mid', 'cost-high'].forEach((k) => {
        if (c.classList.contains(k)) classes.add(k);
      });
    });
    // Fixture has costs 1.5, 2.2, ..., which cover both cost-mid and cost-high
    expect(classes.size).toBeGreaterThanOrEqual(2);
  });

  it('sess-table class on the table element', () => {
    render(<SessionsPanel />);
    expect(document.querySelector('table.sess-table')).not.toBeNull();
  });

  it('typing in the filter input narrows the rows', async () => {
    render(<SessionsPanel />);
    const user = userEvent.setup();
    const btn = document.getElementById('filter-btn') as HTMLButtonElement | null;
    if (btn) await user.click(btn);
    const input = document.getElementById('filter-input') as HTMLInputElement;
    expect(input).toBeTruthy();
    await user.type(input, 'opus{enter}');
    expect(getState().filterText).toBe('opus');
    const rows = document.querySelectorAll('.session-row');
    const snap = fixture as unknown as Envelope;
    const expected = snap.sessions.rows.filter(
      (r) =>
        r.model.toLowerCase().includes('opus') ||
        r.project.toLowerCase().includes('opus'),
    ).length;
    expect(rows.length).toBe(expected);
  });

  it('clicking a row dispatches OPEN_MODAL kind=session', async () => {
    render(<SessionsPanel />);
    const user = userEvent.setup();
    const firstRow = document.querySelector('.session-row') as HTMLElement;
    await user.click(firstRow);
    expect(getState().openModal).toBe('session');
    expect(getState().openSessionId).toBeTruthy();
  });

  it('Escape clears the filter and blur-on-unmount does not undo the clear', async () => {
    dispatch({ type: 'SET_FILTER', text: 'opus' });
    render(<SessionsPanel />);
    const user = userEvent.setup();
    await user.click(document.getElementById('filter-btn')!);
    const input = document.getElementById('filter-input') as HTMLInputElement;
    expect(input.value).toBe('opus');
    await user.keyboard('{Escape}');
    expect(getState().filterText).toBe('');
  });

  it('renders the collapsed filter button as a chip with × when filterText is non-empty', async () => {
    dispatch({ type: 'SET_FILTER', text: 'opus' });
    render(<SessionsPanel />);
    const btn = document.getElementById('filter-btn') as HTMLButtonElement;
    expect(btn.classList.contains('as-chip')).toBe(true);
    expect(btn.querySelector('.chip')?.textContent).toBe('filter: opus');
    expect(btn.querySelector('.chip-x')).not.toBeNull();
    expect(btn.getAttribute('aria-label')).toMatch(/Filter: opus\..*Esc/);
    // Clicking the × clears the filter without expanding the input.
    const user = userEvent.setup();
    await user.click(btn.querySelector('.chip-x') as HTMLElement);
    expect(getState().filterText).toBe('');
    expect(document.getElementById('filter-input')).toBeNull();
    // After clear the collapsed button reverts to the funnel icon.
    const funnel = document.getElementById('filter-btn')?.querySelector('svg use');
    expect(funnel?.getAttribute('href')).toBe('/static/icons.svg#funnel');
  });

  it('filter narrows rows live as the user types (controlled input)', async () => {
    render(<SessionsPanel />);
    const user = userEvent.setup();
    await user.click(document.getElementById('filter-btn')!);
    const input = document.getElementById('filter-input') as HTMLInputElement;
    await user.type(input, 'o');
    expect(getState().filterText).toBe('o');
    await user.type(input, 'pus');
    expect(getState().filterText).toBe('opus');
  });
});

describe('<SessionsPanel /> collapse toggle', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('applies .sessions-collapsed class by default', () => {
    render(<SessionsPanel />);
    const section = document.getElementById('panel-sessions');
    expect(section?.classList.contains('sessions-collapsed')).toBe(true);
  });

  it('removes .sessions-collapsed class after expanding via SAVE_PREFS', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: false } });
    render(<SessionsPanel />);
    const section = document.getElementById('panel-sessions');
    expect(section?.classList.contains('sessions-collapsed')).toBe(false);
  });

  it('renders the chevron toggle button with chevron-down icon when collapsed', () => {
    render(<SessionsPanel />);
    const btn = document.querySelector('.panel-collapse-toggle');
    expect(btn).not.toBeNull();
    expect(btn?.getAttribute('aria-expanded')).toBe('false');
    expect(btn?.getAttribute('aria-controls')).toBe('panel-sessions-body');
    const useEl = btn?.querySelector('svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#chevron-down');
  });

  it('flips icon to chevron-up and aria-expanded=true after expanding', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: false } });
    render(<SessionsPanel />);
    const btn = document.querySelector('.panel-collapse-toggle');
    expect(btn?.getAttribute('aria-expanded')).toBe('true');
    const useEl = btn?.querySelector('svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#chevron-up');
  });

  it('clicking the chevron toggles the pref and persists it', async () => {
    const user = userEvent.setup();
    render(<SessionsPanel />);
    expect(getState().prefs.sessionsCollapsed).toBe(true);
    const btn = document.querySelector('.panel-collapse-toggle') as HTMLElement;
    await user.click(btn);
    expect(getState().prefs.sessionsCollapsed).toBe(false);
    expect(JSON.parse(localStorage.getItem('ccusage.dashboard.prefs')!).sessionsCollapsed).toBe(false);
  });

  it('panel-body has id="panel-sessions-body"', () => {
    render(<SessionsPanel />);
    const body = document.querySelector('#panel-sessions .panel-body');
    expect(body?.id).toBe('panel-sessions-body');
  });
});

describe('<SessionsPanel /> sortable header', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders a <SortableHeader> with five sortable <th> elements', () => {
    render(<SessionsPanel />);
    const ths = document.querySelectorAll('#panel-sessions table.sess-table thead th.th-sortable');
    expect(ths.length).toBe(5);
  });

  it('clicking the Cost header sets sessionsSortOverride to cost desc', async () => {
    const user = userEvent.setup();
    render(<SessionsPanel />);
    const costTh = document.querySelector(
      '#panel-sessions table.sess-table thead th[data-col="cost"]',
    ) as HTMLElement;
    await user.click(costTh);
    expect(getState().prefs.sessionsSortOverride).toEqual({ column: 'cost', direction: 'desc' });
  });

  it('clicking Cost three times cycles through desc → asc → cleared', async () => {
    const user = userEvent.setup();
    render(<SessionsPanel />);
    const costTh = document.querySelector(
      '#panel-sessions table.sess-table thead th[data-col="cost"]',
    ) as HTMLElement;
    await user.click(costTh);
    expect(getState().prefs.sessionsSortOverride).toEqual({ column: 'cost', direction: 'desc' });
    await user.click(costTh);
    expect(getState().prefs.sessionsSortOverride).toEqual({ column: 'cost', direction: 'asc' });
    await user.click(costTh);
    expect(getState().prefs.sessionsSortOverride).toBeNull();
  });

  it('header click does not bubble up to open the session modal', async () => {
    const user = userEvent.setup();
    render(<SessionsPanel />);
    const projectTh = document.querySelector(
      '#panel-sessions table.sess-table thead th[data-col="project"]',
    ) as HTMLElement;
    await user.click(projectTh);
    expect(getState().openModal).toBeNull();
  });

  it('sort-pill click clears any active sessionsSortOverride', async () => {
    const user = userEvent.setup();
    render(<SessionsPanel />);
    // Set an override via header click first.
    const costTh = document.querySelector(
      '#panel-sessions table.sess-table thead th[data-col="cost"]',
    ) as HTMLElement;
    await user.click(costTh);
    expect(getState().prefs.sessionsSortOverride).not.toBeNull();
    // Now click the pill.
    const pill = document.getElementById('sort-pill') as HTMLElement;
    await user.click(pill);
    expect(getState().prefs.sessionsSortOverride).toBeNull();
  });

  it('sort-pill label reflects override when override is active', async () => {
    const user = userEvent.setup();
    render(<SessionsPanel />);
    const projectTh = document.querySelector(
      '#panel-sessions table.sess-table thead th[data-col="project"]',
    ) as HTMLElement;
    await user.click(projectTh);  // sets override to project asc
    const label = document.querySelector('#sort-pill .label')?.textContent ?? '';
    expect(label).toMatch(/project asc/);
  });
});
