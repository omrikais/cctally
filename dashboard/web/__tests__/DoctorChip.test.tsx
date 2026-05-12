import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DoctorChip } from '../src/components/DoctorChip';
import { dispatch, getState, _resetForTests } from '../src/store/store';

describe('<DoctorChip />', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('renders nothing before the first doctor SSE block arrives', () => {
    const { container } = render(<DoctorChip />);
    expect(container.firstChild).toBeNull();
  });

  it('renders an OK pill when severity is ok', () => {
    dispatch({
      type: 'SET_DOCTOR_AGGREGATE',
      doctor: {
        severity: 'ok',
        counts: { ok: 7, warn: 0, fail: 0 },
        generated_at: '2026-05-13T10:00:00Z',
        fingerprint: 'sha1:abc',
      },
    });
    render(<DoctorChip />);
    const btn = screen.getByRole('button', { name: /Doctor diagnostic: OK/i });
    expect(btn.classList.contains('doctor-chip--ok')).toBe(true);
    expect(screen.getByText('Doctor')).toBeInTheDocument();
  });

  it('renders a WARN pill with warn count when severity is warn', () => {
    dispatch({
      type: 'SET_DOCTOR_AGGREGATE',
      doctor: {
        severity: 'warn',
        counts: { ok: 5, warn: 2, fail: 0 },
        generated_at: '2026-05-13T10:00:00Z',
        fingerprint: 'sha1:abc',
      },
    });
    render(<DoctorChip />);
    const btn = screen.getByRole('button', { name: /Doctor diagnostic: WARN/i });
    expect(btn.classList.contains('doctor-chip--warn')).toBe(true);
    expect(screen.getByText(/2 warn/)).toBeInTheDocument();
  });

  it('renders a FAIL pill with fail count when there are failures', () => {
    dispatch({
      type: 'SET_DOCTOR_AGGREGATE',
      doctor: {
        severity: 'fail',
        counts: { ok: 4, warn: 1, fail: 2 },
        generated_at: '2026-05-13T10:00:00Z',
        fingerprint: 'sha1:abc',
      },
    });
    render(<DoctorChip />);
    const btn = screen.getByRole('button', { name: /Doctor diagnostic: FAIL/i });
    expect(btn.classList.contains('doctor-chip--fail')).toBe(true);
    // Fail count supersedes warn count for clarity.
    expect(screen.getByText(/2 fail/)).toBeInTheDocument();
    expect(screen.queryByText(/1 warn/)).not.toBeInTheDocument();
  });

  it('dispatches OPEN_DOCTOR_MODAL on click', () => {
    dispatch({
      type: 'SET_DOCTOR_AGGREGATE',
      doctor: {
        severity: 'ok',
        counts: { ok: 7, warn: 0, fail: 0 },
        generated_at: '2026-05-13T10:00:00Z',
        fingerprint: 'sha1:abc',
      },
    });
    render(<DoctorChip />);
    expect(getState().doctorModalOpen).toBe(false);
    fireEvent.click(screen.getByRole('button'));
    expect(getState().doctorModalOpen).toBe(true);
  });
});
