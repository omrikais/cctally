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
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import {
  dispatch,
  getState,
  subscribeStore,
  topmostStoreFocusLayer,
} from '../store/store';
import { useKeymap } from '../hooks/useKeymap';
import { useModalFocus } from '../hooks/useModalFocus';
import { useScrollLock } from '../hooks/useScrollLock';
import { ModalHeader } from '../modals/ModalHeader';
import { ModalCloseButton } from '../modals/ModalCloseButton';
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

  // DOC-2 (#253): reconcile the SSE-fed Doctor chip to the freshest report.
  // /api/doctor recomputes fresh (like the SSE block), so a manual fetch can
  // be newer than the chip's last SSE tick. Feed it back into the store slice
  // the chip reads. Guard on generated_at so a slow in-flight fetch can't
  // clobber a newer SSE tick. /api/doctor has no `fingerprint`; nothing on the
  // client consumes it for display and the next SSE tick overwrites this
  // transient value, so a synthetic marker is honest and safe.
  useEffect(() => {
    if (!report) return;
    const prev = getState().doctor;
    const prevAt = prev ? Date.parse(prev.generated_at) : NaN;
    const nextAt = Date.parse(report.generated_at);
    if (prev && Number.isFinite(prevAt) && Number.isFinite(nextAt) && nextAt <= prevAt) return;
    dispatch({
      type: 'SET_DOCTOR_AGGREGATE',
      doctor: {
        severity: report.overall.severity,
        counts: report.overall.counts,
        generated_at: report.generated_at,
        fingerprint: 'client-reconcile',
      },
    });
  }, [report]);

  // a11y focus management (#207 A1). Called BEFORE the `!open` early-return so
  // the hook order stays stable (Rules of Hooks). `active: open` drives
  // focus-in/restore; the Tab-trap suspends when a higher store-tracked layer
  // opens above the doctor modal.
  const cardRef = useRef<HTMLDivElement>(null);
  const trapEnabled = useSyncExternalStore(
    subscribeStore,
    () => topmostStoreFocusLayer(getState()) === 'doctor',
  );
  // initialFocus: 'container' (not the default 'first'): the first focusable —
  // the refresh button — self-disables on open (the auto-fired refresh() sets
  // loading=true synchronously). A focused element that becomes disabled is
  // blurred by the browser, dropping focus to <body>. Focusing the container
  // (tabIndex=-1, never disabled) keeps focus inside the dialog.
  useModalFocus(cardRef, { active: open, trapEnabled, initialFocus: 'container' });

  // M1-1: lock background page scroll while the doctor modal is open.
  // Declared BEFORE the `!open` early-return so the hook order stays stable.
  useScrollLock(open);

  if (!open) return null;

  return (
    <div className="update-modal-root" onClick={close}>
      <div className="modal-backdrop" />
      <div
        ref={cardRef}
        className="modal-card doctor-modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="doctor-modal-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <ModalHeader
          title="Doctor"
          titleId="doctor-modal-title"
          headerExtras={
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
              {/* Close stays INSIDE the flex `header-actions` div (8px gap)
                  rather than ModalHeader's own trailing slot, so the refresh
                  ↔ close spacing is preserved (#216). */}
              <ModalCloseButton onClose={close} />
            </div>
          }
        />
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

export function DoctorCategoryRow({
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
        {/* DOC-1: disclosure caret matching the per-check "▶/▼ details"
            affordance so the collapsible category reads as expandable. */}
        <span className="doctor-modal__category-caret" aria-hidden="true">
          {openCat ? '▾' : '▸'}
        </span>
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

export function DoctorCheckRow({ c }: { c: DoctorCheck }): JSX.Element {
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
      {c.severity !== 'ok' && !c.remediation && (
        <p className="doctor-modal__remediation">→ Run <code>cctally doctor</code> for the full report</p>
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
