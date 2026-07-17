import { beforeEach, describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { SourceSwitcher } from './SourceSwitcher';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function radios(): HTMLElement[] {
  return within(screen.getByRole('radiogroup')).getAllByRole('radio');
}

describe('SourceSwitcher — radiogroup semantics (§5.4)', () => {
  it('is a labelled radiogroup with three radios; Claude checked by default', () => {
    render(<SourceSwitcher />);
    const group = screen.getByRole('radiogroup', { name: /data source/i });
    expect(group).toBeInTheDocument();
    const rs = radios();
    expect(rs).toHaveLength(3);
    expect(rs[0]).toHaveAttribute('aria-checked', 'true'); // claude
    expect(rs[1]).toHaveAttribute('aria-checked', 'false');
    expect(rs[2]).toHaveAttribute('aria-checked', 'false');
  });

  it('has exactly one roving tab stop (the checked segment)', () => {
    render(<SourceSwitcher />);
    const rs = radios();
    expect(rs[0]).toHaveAttribute('tabindex', '0');
    expect(rs[1]).toHaveAttribute('tabindex', '-1');
    expect(rs[2]).toHaveAttribute('tabindex', '-1');
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    const rs2 = radios();
    expect(rs2[1]).toHaveAttribute('tabindex', '0');
    expect(rs2[0]).toHaveAttribute('tabindex', '-1');
  });

  it('clicking a segment dispatches SET_ACTIVE_SOURCE', () => {
    render(<SourceSwitcher />);
    fireEvent.click(radios()[2]);
    expect(getState().activeSource).toBe('all');
  });

  it('ArrowRight/ArrowLeft move focus + selection together, wrapping at the ends', () => {
    render(<SourceSwitcher />);
    radios()[0].focus();
    fireEvent.keyDown(radios()[0], { key: 'ArrowRight' });
    expect(getState().activeSource).toBe('codex');
    expect(document.activeElement).toBe(radios()[1]);
    // wrap forward: all → claude
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    radios()[2].focus();
    fireEvent.keyDown(radios()[2], { key: 'ArrowRight' });
    expect(getState().activeSource).toBe('claude');
    // wrap backward: claude → all
    fireEvent.keyDown(radios()[0], { key: 'ArrowLeft' });
    expect(getState().activeSource).toBe('all');
  });

  it('Home/End jump to first/last', () => {
    render(<SourceSwitcher />);
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    fireEvent.keyDown(radios()[1], { key: 'End' });
    expect(getState().activeSource).toBe('all');
    fireEvent.keyDown(radios()[2], { key: 'Home' });
    expect(getState().activeSource).toBe('claude');
  });

  it('has NO disabled segments — an unavailable source stays selectable and names its availability', () => {
    const slice = makeSourceEnvelope();
    slice.sources.codex = {
      ...slice.sources.codex,
      availability: 'unavailable',
      warnings: [{ code: 'x', message: 'gone' }],
    };
    updateSnapshot(slice as unknown as Envelope);
    render(<SourceSwitcher />);
    const rs = radios();
    expect(rs[1]).not.toBeDisabled();
    expect(rs[1]).toHaveAttribute('aria-label', expect.stringContaining('unavailable'));
    fireEvent.click(rs[1]);
    expect(getState().activeSource).toBe('codex'); // still selectable
  });

  it('renders a polite aria-live region whose text updates on switch', () => {
    render(<SourceSwitcher />);
    const live = screen.getByTestId('source-switcher-live');
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live).toHaveTextContent('');
    fireEvent.click(radios()[1]);
    expect(live).toHaveTextContent(/codex source selected/i);
  });
});

describe('SourceSwitcher — dashboard workspace only (§5.4)', () => {
  it('renders nothing in the conversations view', () => {
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    const { container } = render(<SourceSwitcher />);
    expect(container.querySelector('.source-switcher')).toBeNull();
  });
});
