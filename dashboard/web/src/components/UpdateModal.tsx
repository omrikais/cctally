import { useMemo, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useKeymap } from '../hooks/useKeymap';
import { UpdateIdleModal } from './UpdateIdleModal';
import { UpdateRunningModal } from './UpdateRunningModal';
import { UpdateSuccessModal } from './UpdateSuccessModal';
import { UpdateFailedModal } from './UpdateFailedModal';

// Update-modal router. Uses its own `update.modalOpen` flag (not the
// generic openModal/ModalKind machinery) so the modal can persist a
// running run across a tab close+reopen without colliding with
// panel-modal kinds, AND so the success state can stay mounted across
// the post-execvp dashboard restart.
//
// Frame visuals match the existing `.modal-card` chrome (dark panel
// with accent border + Esc dismiss + click-backdrop dismiss) but we
// route the close action through our slice's CLOSE_UPDATE_MODAL so
// the run state is preserved.
//
// The Esc binding is registered via useKeymap with scope='modal' so it
// wins over the global digit/letter bindings (modal scope is sorted
// first in the keymap dispatch order). We pass `when` as a guard so the
// binding is inert when modalOpen flips false during teardown.

const TITLE_BY_STATUS: Record<string, string> = {
  idle: 'Update available',
  running: 'Updating cctally',
  success: 'Update complete',
  failed: 'Update failed',
};

const ACCENT_BY_STATUS: Record<string, string> = {
  idle: 'accent-amber',
  running: 'accent-amber',
  success: 'accent-green',
  failed: 'accent-red',
};

export function UpdateModal() {
  const update = useSyncExternalStore(subscribeStore, () => getState().update);
  const close = () => dispatch({ type: 'CLOSE_UPDATE_MODAL' });
  const bindings = useMemo(
    () => [{ key: 'Escape', scope: 'modal' as const, action: close }],
    [],
  );
  useKeymap(bindings);

  if (!update.modalOpen) return null;
  if (!update.state) return null;

  const status = update.status;
  const accent = ACCENT_BY_STATUS[status] ?? 'accent-amber';
  let title = TITLE_BY_STATUS[status] ?? 'Update';
  if (status === 'running' && update.state.latest_version) {
    title = `Updating cctally → ${update.state.latest_version}`;
  } else if (status === 'success' && update.state.latest_version) {
    title = `Update complete → ${update.state.latest_version}`;
  }

  let body: JSX.Element;
  switch (status) {
    case 'running':
      body = <UpdateRunningModal />;
      break;
    case 'success':
      body = <UpdateSuccessModal />;
      break;
    case 'failed':
      body = <UpdateFailedModal />;
      break;
    case 'idle':
    default:
      body = <UpdateIdleModal />;
      break;
  }

  return (
    <div id="update-modal-root" className="update-modal-root">
      <div className="modal-backdrop" onClick={close} />
      <div
        className={`modal-card update-modal-card ${accent}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="update-modal-title"
      >
        <div className="modal-handle" aria-hidden="true" />
        <header className="modal-header">
          <h2 id="update-modal-title">{title}</h2>
          <button
            className="modal-close"
            aria-label="Close"
            onClick={close}
          >
            ×
          </button>
        </header>
        <div className="modal-body">{body}</div>
      </div>
    </div>
  );
}
