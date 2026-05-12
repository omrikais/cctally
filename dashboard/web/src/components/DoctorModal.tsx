// Full doctor report modal (spec §6.3).
//
// Triggered by the DoctorChip click and the `d` keymap (registered in
// main.tsx with the composite guard spec §6.4 mandates). Opens on
// `state.doctorModalOpen === true` (NOT folded into openModal — the
// composite guard requires the modal flag to be readable independently
// of the panel-modal slot).
//
// Lifecycle:
//   open → useDoctorReport.refresh() is fired once via useEffect.
//   Refresh button → manual re-fetch. No auto-refresh on fingerprint
//   change for v1 (spec §6.3 reading-friendly).
//   Esc → CLOSE_DOCTOR_MODAL. Backdrop click → same. Both routes
//   share the same `close` callback.
//
// Layout follows the kernel's category-then-check tree:
//   - <header> with title + refresh button + close button.
//   - <p> aggregate summary line ("N OK · M WARN · K FAIL").
//   - <section> per category, accordion-collapsed when severity=ok,
//     expanded by default when severity != ok (spec §6.3 "non-OK
//     categories auto-expanded").
//   - <div> per check with glyph + title + summary + remediation +
//     "▶ details" disclosure for the raw details JSON.
//
// The Esc binding goes through the same useKeymap modal-scope route
// the UpdateModal uses, gated on `doctorModalOpen` so the binding
// stays inert when the modal is closed.
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useKeymap } from '../hooks/useKeymap';
import {
  useDoctorReport,
  type DoctorCategory,
  type DoctorCheck,
  type DoctorReport,
} from '../hooks/useDoctorReport';

const GLYPH: Record<'ok' | 'warn' | 'fail', string> = {
  ok: '✓',     // ✓
  warn: '⚠',   // ⚠
  fail: '✗',   // ✗
};

export function DoctorModal(): JSX.Element | null {
  const open = useSyncExternalStore(
    subscribeStore,
    () => getState().doctorModalOpen,
  );
  const { report, loading, error, refresh } = useDoctorReport();

  const close = (): void => { dispatch({ type: 'CLOSE_DOCTOR_MODAL' }); };

  // Esc binding via the modal-scope keymap so it wins over the
  // global digit/letter bindings; `when:` keeps it inert while the
  // modal is closed (DoctorModal mounts for the app's lifetime —
  // without the gate the binding would fire whenever the user
  // hit Esc with no modal up, swallowing the event before other
  // owners could see it).
  const bindings = useMemo(
    () => [{
      key: 'Escape',
      scope: 'modal' as const,
      action: close,
      when: () => getState().doctorModalOpen,
    }],
    [],
  );
  useKeymap(bindings);

  // Fire one refresh on open. Subsequent opens (close → reopen) also
  // refetch so the user sees a current report rather than a stale
  // cached one. Manual refresh button is in the header.
  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  if (!open) return null;

  return (
    <div className="update-modal-root" onClick={close}>
      <div className="modal-backdrop" />
      <div
        className="modal-card doctor-modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="doctor-modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h2 id="doctor-modal-title">Doctor</h2>
          <div className="doctor-modal__header-actions">
            <button
              type="button"
              className="doctor-modal__refresh"
              onClick={() => { void refresh(); }}
              disabled={loading}
              aria-label="Refresh doctor report"
            >
              {loading ? 'refreshing…' : '↻ refresh'}
            </button>
            <button
              type="button"
              className="modal-close"
              aria-label="Close"
              onClick={close}
            >
              ×
            </button>
          </div>
        </header>
        <div className="modal-body">
          {loading && !report && <p>Loading…</p>}
          {error && (
            <p className="doctor-modal__error">Error loading report: {error}</p>
          )}
          {report && <DoctorReportBody report={report} />}
        </div>
      </div>
    </div>
  );
}

function DoctorReportBody({ report }: { report: DoctorReport }): JSX.Element {
  const { counts } = report.overall;
  return (
    <>
      <p className="doctor-modal__summary">
        {counts.ok} OK · {counts.warn} WARN · {counts.fail} FAIL
      </p>
      {report.categories.map((cat) => (
        <DoctorCategoryRow
          key={cat.id}
          cat={cat}
          defaultOpen={cat.severity !== 'ok'}
        />
      ))}
    </>
  );
}

function DoctorCategoryRow({
  cat,
  defaultOpen,
}: { cat: DoctorCategory; defaultOpen: boolean }): JSX.Element {
  const [openCat, setOpenCat] = useState(defaultOpen);
  return (
    <section className={`doctor-modal__category doctor-modal__category--${cat.severity}`}>
      <button
        type="button"
        className="doctor-modal__category-header"
        onClick={() => setOpenCat((s) => !s)}
        aria-expanded={openCat}
      >
        <span className="doctor-modal__glyph" aria-hidden="true">
          {GLYPH[cat.severity]}
        </span>
        <span>{cat.title}</span>
      </button>
      {openCat && cat.checks.map((c) => (
        <DoctorCheckRow key={c.id} c={c} />
      ))}
    </section>
  );
}

function DoctorCheckRow({ c }: { c: DoctorCheck }): JSX.Element {
  const [showDetails, setShowDetails] = useState(false);
  const hasDetails = c.details && Object.keys(c.details).length > 0;
  return (
    <div className={`doctor-modal__check doctor-modal__check--${c.severity}`}>
      <p>
        <span className="doctor-modal__glyph" aria-label={c.severity}>
          {GLYPH[c.severity]}
        </span>{' '}
        <strong>{c.title}</strong>: {c.summary}
      </p>
      {c.remediation && (
        <p className="doctor-modal__remediation">→ {c.remediation}</p>
      )}
      {hasDetails && (
        <>
          <button
            type="button"
            className="doctor-modal__details-toggle"
            onClick={() => setShowDetails((s) => !s)}
            aria-expanded={showDetails}
          >
            {showDetails ? '▼' : '▶'} details
          </button>
          {showDetails && (
            <pre className="doctor-modal__details">
              {JSON.stringify(c.details, null, 2)}
            </pre>
          )}
        </>
      )}
    </div>
  );
}
