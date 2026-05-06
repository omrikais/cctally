import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { BlocksPanel } from '../src/panels/BlocksPanel';
import { updateSnapshot, _resetForTests, dispatch } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<BlocksPanel />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the panel card with blue accent', () => {
    render(<BlocksPanel />);
    const section = document.getElementById('panel-blocks');
    expect(section?.classList.contains('panel')).toBe(true);
    expect(section?.classList.contains('accent-blue')).toBe(true);
  });

  it('renders the layers icon and the "current week" subtitle', () => {
    render(<BlocksPanel />);
    const useEl = document.querySelector('#panel-blocks svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#layers');
    expect(screen.getByText(/current week/i)).toBeInTheDocument();
  });

  it('renders one .blocks-row per fixture row (3 rows)', () => {
    render(<BlocksPanel />);
    const rows = document.querySelectorAll('#panel-blocks .blocks-row');
    expect(rows.length).toBe(3);
  });

  it('renders the Active pill on the active row only', () => {
    render(<BlocksPanel />);
    const pills = document.querySelectorAll('#panel-blocks .pill-active');
    expect(pills.length).toBe(1);
  });

  it('prefixes heuristic-anchor labels with ~', () => {
    render(<BlocksPanel />);
    const labels = Array.from(document.querySelectorAll('#panel-blocks .blocks-row .label'));
    const heuristic = labels.find((el) => el.textContent?.includes('19:00 Apr 25'));
    expect(heuristic?.textContent).toContain('~');
  });

  it('does NOT prefix recorded-anchor labels with ~', () => {
    render(<BlocksPanel />);
    const labels = Array.from(document.querySelectorAll('#panel-blocks .blocks-row .label'));
    const recorded = labels.find((el) => el.textContent?.includes('14:00 Apr 26'));
    expect(recorded?.textContent?.startsWith('~')).toBe(false);
  });

  it('gauge widths are proportional to cost / max_cost', async () => {
    render(<BlocksPanel />);
    await waitFor(() => {
      const fills = document.querySelectorAll('#panel-blocks .gauge-fill') as NodeListOf<HTMLElement>;
      expect(fills[0].style.width).toBe('100%');
    });
    const fills = document.querySelectorAll('#panel-blocks .gauge-fill') as NodeListOf<HTMLElement>;
    const n = (s: string) => parseFloat(s.replace('%', ''));
    expect(Math.round(n(fills[1].style.width))).toBe(44);
    expect(Math.round(n(fills[2].style.width))).toBe(15);
  });

  it('shows ~ legend in footer when any heuristic row present', () => {
    render(<BlocksPanel />);
    const foot = document.querySelector('#panel-blocks .panel-foot');
    expect(foot?.textContent).toMatch(/~ = approximate start/);
  });

  it('applies blocks-collapsed class when prefs.blocksCollapsed is true', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { blocksCollapsed: true } });
    render(<BlocksPanel />);
    const section = document.getElementById('panel-blocks');
    expect(section?.classList.contains('blocks-collapsed')).toBe(true);
  });

  it('omits blocks-collapsed class when prefs.blocksCollapsed is false', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { blocksCollapsed: false } });
    render(<BlocksPanel />);
    const section = document.getElementById('panel-blocks');
    expect(section?.classList.contains('blocks-collapsed')).toBe(false);
  });

  it('shows panel-empty when rows is empty', () => {
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      blocks: { rows: [] },
    });
    render(<BlocksPanel />);
    expect(screen.getByText(/No activity blocks this week yet/i)).toBeInTheDocument();
  });
});
