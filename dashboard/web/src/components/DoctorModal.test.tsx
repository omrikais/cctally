import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DoctorCheckRow, DoctorCategoryRow, DoctorModal } from './DoctorModal';
import { _resetForTests, dispatch, getState } from '../store/store';
import type { DoctorReport } from '../hooks/useDoctorReport';

// Control the modal's fresh GET /api/doctor result. `useDoctorReport` is a
// pure fetch hook; mocking it lets us feed a report with an arbitrary
// generated_at to exercise the DOC-2 freshness guard without a real fetch.
let mockDoctorReport: DoctorReport | null = null;
vi.mock('../hooks/useDoctorReport', () => ({
  useDoctorReport: () => ({ report: mockDoctorReport, loading: false, error: null, refresh: vi.fn() }),
}));

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  mockDoctorReport = null;
});

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

describe('DoctorCheckRow humanizes Latest snapshot age (#259)', () => {
  it('renders the humanized age instead of the raw server "Ns ago" summary', () => {
    const { container } = render(
      <DoctorCheckRow
        c={{
          id: 'data.latest_snapshot_age',
          title: 'Latest snapshot',
          severity: 'warn',
          summary: '97765s ago',
          details: { latest_snapshot_at: '2026-06-29T06:47:52Z', latest_snapshot_age_s: 97765 },
        }}
      />
    );
    // 97765s ≈ 27h → "1d 3h ago"; the raw seconds must NOT leak into the line.
    const line = container.querySelector('.doctor-modal__check > p') as HTMLElement;
    expect(line.textContent).toContain('1d 3h ago');
    expect(line.textContent).not.toContain('97765s ago');
  });
  it('falls back to the server summary when the age detail is absent ("none recorded")', () => {
    render(
      <DoctorCheckRow
        c={{
          id: 'data.latest_snapshot_age',
          title: 'Latest snapshot',
          severity: 'warn',
          summary: 'none recorded',
          details: { latest_snapshot_at: null },
        }}
      />
    );
    expect(screen.getByText(/none recorded/)).toBeTruthy();
  });
  it('does NOT humanize other checks that carry raw "Ns ago" summaries', () => {
    render(
      <DoctorCheckRow
        c={{
          id: 'hooks.last_fire_age',
          title: 'Last hook fire',
          severity: 'ok',
          summary: '42s ago',
          details: { last_fire_age_s: 42 },
        }}
      />
    );
    // Out of #259's scope — this surface stays verbatim.
    expect(screen.getByText(/42s ago/)).toBeTruthy();
  });
});

describe('DoctorCategoryRow caret (DOC-1)', () => {
  it('renders a caret that reflects and toggles aria-expanded', () => {
    const cat = { id: 'auth', title: 'Auth', severity: 'ok' as const, checks: [] };
    const { container, getByRole } = render(<DoctorCategoryRow cat={cat} defaultOpen={false} />);
    const btn = getByRole('button');
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    const caret = container.querySelector('.doctor-modal__category-caret');
    expect(caret?.textContent).toBe('▸');
    fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(container.querySelector('.doctor-modal__category-caret')?.textContent).toBe('▾');
  });
});

describe('Doctor chip reconcile (DOC-2)', () => {
  it('dispatches SET_DOCTOR_AGGREGATE when the report is newer than the slice', () => {
    dispatch({
      type: 'SET_DOCTOR_AGGREGATE',
      doctor: { severity: 'warn', counts: { ok: 20, warn: 6, fail: 0 }, generated_at: '2026-07-02T10:00:00Z', fingerprint: 'sha1:x' },
    });
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    mockDoctorReport = {
      schema_version: 1, generated_at: '2026-07-02T10:05:00Z', cctally_version: 'x',
      overall: { severity: 'fail', counts: { ok: 20, warn: 5, fail: 1 } }, categories: [],
    };
    render(<DoctorModal />);
    expect(getState().doctor).toEqual({
      severity: 'fail', counts: { ok: 20, warn: 5, fail: 1 },
      generated_at: '2026-07-02T10:05:00Z', fingerprint: 'client-reconcile',
    });
  });
  it('does not dispatch when the report is older-or-equal to the slice', () => {
    dispatch({
      type: 'SET_DOCTOR_AGGREGATE',
      doctor: { severity: 'fail', counts: { ok: 20, warn: 5, fail: 1 }, generated_at: '2026-07-02T10:05:00Z', fingerprint: 'sha1:x' },
    });
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    mockDoctorReport = {
      schema_version: 1, generated_at: '2026-07-02T10:00:00Z', cctally_version: 'x',
      overall: { severity: 'warn', counts: { ok: 20, warn: 6, fail: 0 } }, categories: [],
    };
    render(<DoctorModal />);
    // Unchanged — the SSE-fed slice is at least as fresh, so the guard skips.
    expect(getState().doctor?.fingerprint).toBe('sha1:x');
    expect(getState().doctor?.severity).toBe('fail');
  });
});
