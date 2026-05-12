// Doctor aggregate-status chip in the header (spec §6.1).
//
// Reads the SSE-mirrored `doctor` slice (sse.ts dispatches
// SET_DOCTOR_AGGREGATE on every tick that carries snap.doctor). Renders
// a small OK/WARN/FAIL pill colored by `--accent-green` / `--accent-amber`
// / `--accent-red` to match the existing pill-warn / badge-update palette.
//
// The pill text adapts to the worst severity:
//   ok   → "Doctor"
//   warn → "Doctor · N warn"     (when no failures)
//   fail → "Doctor · N fail"     (suppresses warn count for clarity)
//
// Clicking the chip opens the DoctorModal via OPEN_DOCTOR_MODAL — the
// `d` keymap in main.tsx dispatches the same action. The composite
// guard (no other modal, no input mode) lives on the keymap side
// (spec §6.4 / Codex M5) — clicks always succeed since the chip is
// the modal's primary affordance.
//
// Hidden until the first SSE tick arrives carrying the doctor block
// (matches the UpdateBadge "absent until known" posture; avoids a
// "Doctor · …loading" flash on cold boot).
import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';

const SEVERITY_LABEL: Record<'ok' | 'warn' | 'fail', string> = {
  ok: 'OK',
  warn: 'WARN',
  fail: 'FAIL',
};

export function DoctorChip(): JSX.Element | null {
  const doctor = useSyncExternalStore(subscribeStore, () => getState().doctor);
  if (!doctor) return null;

  const { severity, counts, generated_at } = doctor;
  let label = 'Doctor';
  if (counts.fail > 0) {
    label = `Doctor · ${counts.fail} fail`;
  } else if (severity === 'warn' && counts.warn > 0) {
    label = `Doctor · ${counts.warn} warn`;
  }
  const aria = `Doctor diagnostic: ${SEVERITY_LABEL[severity]} — ${counts.ok} OK, ${counts.warn} warn, ${counts.fail} fail`;
  let tooltip = `Doctor · ${SEVERITY_LABEL[severity]} (click for report)`;
  try {
    const when = new Date(generated_at).toLocaleTimeString();
    tooltip = `Doctor · ${SEVERITY_LABEL[severity]} · checked ${when} (click for report)`;
  } catch {
    /* fall through to terse tooltip on a bad timestamp */
  }

  return (
    <button
      type="button"
      className={`doctor-chip doctor-chip--${severity}`}
      title={tooltip}
      aria-label={aria}
      onClick={() => dispatch({ type: 'OPEN_DOCTOR_MODAL' })}
    >
      <span className="doctor-chip__dot" aria-hidden="true">●</span>
      <span className="doctor-chip__label">{label}</span>
    </button>
  );
}
