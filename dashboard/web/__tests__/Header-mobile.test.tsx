import { describe, expect, it, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Header } from '../src/components/Header';
import { _resetForTests, updateSnapshot } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('Header — mobile additions', () => {
  // #248 — the five dashboard stats (incl. 5h-percent + "vs last week") left
  // the header for the HeroStrip, so the mobile data-mobile-keep ordering of
  // those stats is gone too. What remains here is the action-button chrome.
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
});
