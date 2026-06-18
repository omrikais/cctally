import { describe, expect, it, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Header } from '../src/components/Header';
import { _resetForTests, updateSnapshot } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('Header — mobile additions', () => {
  // Seed a snapshot so the value-gated stats render (#207 B1: the
  // "vs last week" stat hides when its delta is null; the fixture's
  // delta is non-null, so the stat — and its data-mobile-keep — renders).
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders ⚙ settings and ? help icon buttons', () => {
    render(<Header />);
    const settings = screen.getByRole('button', { name: /open settings/i });
    const help = screen.getByRole('button', { name: /open help/i });
    expect(settings).toBeTruthy();
    expect(help).toBeTruthy();
  });

  it('settings click dispatches a synthetic "s" keydown', () => {
    const events: string[] = [];
    const listener = (e: KeyboardEvent) => events.push(e.key);
    document.addEventListener('keydown', listener);
    render(<Header />);
    fireEvent.click(screen.getByRole('button', { name: /open settings/i }));
    document.removeEventListener('keydown', listener);
    expect(events).toContain('s');
  });

  it('help click dispatches a synthetic "?" keydown', () => {
    const events: string[] = [];
    const listener = (e: KeyboardEvent) => events.push(e.key);
    document.addEventListener('keydown', listener);
    render(<Header />);
    fireEvent.click(screen.getByRole('button', { name: /open help/i }));
    document.removeEventListener('keydown', listener);
    expect(events).toContain('?');
  });

  it('marks 5h-percent and "vs last week" stats as data-mobile-keep="secondary"', () => {
    const { container } = render(<Header />);
    const fiveHourStat = container.querySelector('[data-mobile-keep="secondary"][data-stat="five-hour"]');
    const trendStat = container.querySelector('[data-mobile-keep="secondary"][data-stat="vs-last-week"]');
    expect(fiveHourStat).toBeTruthy();
    expect(trendStat).toBeTruthy();
  });
});
