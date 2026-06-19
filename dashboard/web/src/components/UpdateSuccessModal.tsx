import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { refreshUpdateState } from '../store/update';

// "Update complete → restarting" modal (visual companion State 4a).
// Renders a checkmark + the elapsed time + a spinner with "reconnecting".
// The auto-close fires from refreshUpdateState() when current_version ===
// latest_version after the post-execvp /api/data and /api/update/status
// reconnect.
//
// We re-poll every 1.5s so even if the SSE channel takes a moment to
// re-establish (or the polling-fallback code path didn't latch on), the
// modal still closes within ~3s of the new server coming up.

const POST_EXECVP_POLL_MS = 1500;
// Cap the post-execvp reconnect poll so a server that never returns the new
// version can't spin the success modal forever (#207 D7). ~20 polls ≈ 30s.
const POST_EXECVP_MAX_POLLS = 20;

function _formatElapsed(startedAt: number | null): string {
  if (startedAt == null) return '—';
  const s = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${r}s`;
}

export function UpdateSuccessModal() {
  const update = useSyncExternalStore(subscribeStore, () => getState().update);
  const startedAt = update.startedAt;
  const cmd = update.state?.update_command ?? '';
  const latest = update.state?.latest_version ?? '';

  const polls = useRef(0);
  const [timedOut, setTimedOut] = useState(false);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (polls.current >= POST_EXECVP_MAX_POLLS) {
        window.clearInterval(id);
        setTimedOut(true);
        return;
      }
      polls.current += 1;
      refreshUpdateState();
    }, POST_EXECVP_POLL_MS);
    return () => window.clearInterval(id);
  }, []);

  return (
    <div className="update-modal-body">
      <div className="update-running-row update-running-row-success">
        <span className="update-check" aria-hidden="true">
          ✓
        </span>
        <code className="update-cmd update-cmd-inline">{cmd || 'cctally'}</code>
        <span className="update-elapsed">finished in {_formatElapsed(startedAt)}</span>
      </div>
      <div className="update-row-label">
        Restarting dashboard on the new code{latest ? ` (${latest})` : ''}…
      </div>
      {timedOut ? (
        <>
          <div className="update-running-row">
            <span className="update-success-reconnect">
              Still reconnecting — check update.log
            </span>
          </div>
          <button
            type="button"
            className="update-btn"
            onClick={() => {
              dispatch({ type: 'CLOSE_UPDATE_MODAL' });
              dispatch({ type: 'RESET_UPDATE_RUN' });
            }}
          >
            Close
          </button>
        </>
      ) : (
        <>
          <div className="update-running-row">
            <span className="update-spinner" aria-hidden="true">
              ⟳
            </span>
            <span className="update-success-reconnect">
              reconnecting (this page will refresh in a moment)
            </span>
          </div>
          <p className="modal-hint">Press Esc to close</p>
        </>
      )}
    </div>
  );
}
