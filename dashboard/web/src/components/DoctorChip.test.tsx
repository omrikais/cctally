// #293 S4 (§4c) — the doctor chip label is split into a `Doctor` WORD element
// and an ALWAYS-PRESENT compact status element (OK / warn-count / fail-count —
// never a bare dot). At ≤360px the word hides (CSS) and the status stays, so the
// chip always keeps a color-coded status glyph even in the tight FAIL+basket
// state. Non-vacuous: with the old single-label markup there is no separable
// `.doctor-chip-status` element (and none in the OK state at all) → RED.
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { DoctorChip } from './DoctorChip';
import {
  _resetForTests,
  dispatch,
  getState,
  type DoctorAggregate,
} from '../store/store';

function doctor(
  severity: 'ok' | 'warn' | 'fail',
  counts: { ok: number; warn: number; fail: number },
): DoctorAggregate {
  return { severity, counts, generated_at: '2026-07-14T10:00:00Z', fingerprint: 'fp' };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function renderWith(agg: DoctorAggregate) {
  dispatch({ type: 'SET_DOCTOR_AGGREGATE', doctor: agg });
  return render(<DoctorChip />);
}

describe('#293 S4 — DoctorChip label/status split', () => {
  it('OK: separable Doctor word + an always-present, non-empty status token', () => {
    const { container } = renderWith(doctor('ok', { ok: 5, warn: 0, fail: 0 }));
    const label = container.querySelector('.doctor-chip-label');
    const status = container.querySelector('.doctor-chip-status');
    expect(label?.textContent).toBe('Doctor');
    expect(status, 'status element must exist in EVERY state (incl. OK)').not.toBeNull();
    expect((status?.textContent ?? '').trim().length).toBeGreaterThan(0);
    // Not a bare dot — a real status token.
    expect(status?.textContent).not.toBe('●');
    // The old dot markup is gone.
    expect(container.querySelector('.doctor-chip__dot')).toBeNull();
    expect(container.querySelector('.doctor-chip__label')).toBeNull();
  });

  it('warn: the status carries the warn count', () => {
    const { container } = renderWith(doctor('warn', { ok: 3, warn: 2, fail: 0 }));
    expect(container.querySelector('.doctor-chip-label')?.textContent).toBe('Doctor');
    expect(container.querySelector('.doctor-chip-status')?.textContent).toContain('2');
  });

  it('fail: the status carries the fail count', () => {
    const { container } = renderWith(doctor('fail', { ok: 1, warn: 1, fail: 3 }));
    expect(container.querySelector('.doctor-chip-label')?.textContent).toBe('Doctor');
    expect(container.querySelector('.doctor-chip-status')?.textContent).toContain('3');
  });

  it('preserves severity class, aria-label, and click → OPEN_DOCTOR_MODAL', () => {
    const { container } = renderWith(doctor('fail', { ok: 1, warn: 0, fail: 2 }));
    const btn = container.querySelector('button.doctor-chip') as HTMLButtonElement;
    expect(btn.className).toContain('doctor-chip--fail');
    expect(btn.getAttribute('aria-label')).toContain('Doctor diagnostic');
    btn.click();
    expect(getState().doctorModalOpen).toBe(true);
  });
});
