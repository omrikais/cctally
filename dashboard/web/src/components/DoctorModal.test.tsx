import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DoctorCheckRow } from './DoctorModal';

// Matches the fallback paragraph by its normalized textContent (the
// `cctally doctor` <code> splits the string across element boundaries).
const fallbackMatcher = (_content: string, el: Element | null): boolean =>
  el?.tagName === 'P' &&
  /Run\s+cctally doctor\s+for the full report/i.test(el.textContent ?? '');

describe('DoctorCheckRow non-OK fallback (#207 D3)', () => {
  it('shows a fallback for a non-OK check without remediation', () => {
    render(<DoctorCheckRow c={{ id: 'x', title: 'Pricing', severity: 'fail', summary: 'stale', details: {} }} />);
    // The fallback wraps `cctally doctor` in a <code>, so the text is split
    // across element boundaries — match the whole remediation paragraph by its
    // normalized textContent rather than a string matcher.
    const fallback = screen.getByText(fallbackMatcher);
    expect(fallback).toBeTruthy();
    // And the code span carries the command literally.
    expect(fallback.querySelector('code')?.textContent).toBe('cctally doctor');
  });
  it('does NOT show the fallback for an OK check', () => {
    render(<DoctorCheckRow c={{ id: 'y', title: 'OAuth', severity: 'ok', summary: 'fine', details: {} }} />);
    expect(screen.queryByText(fallbackMatcher)).toBeNull();
  });
  it('keeps a real remediation when present', () => {
    render(<DoctorCheckRow c={{ id: 'z', title: 'DB', severity: 'warn', summary: 'x', remediation: 'run db recover', details: {} }} />);
    expect(screen.getByText(/run db recover/)).toBeTruthy();
    expect(screen.queryByText(fallbackMatcher)).toBeNull();
  });
});
