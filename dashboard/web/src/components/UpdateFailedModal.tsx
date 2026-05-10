import { useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import type { UpdateStreamEvent } from '../store/store';
import { startUpdate, updateActions } from '../store/update';

// "Update failed" modal (visual companion State 4b). Shows the last N
// stderr lines from the captured stream, plus three buttons:
//   Retry        — RESET_UPDATE_RUN + startUpdate() again. The worker
//                  is single-shot so we have to issue a fresh POST.
//   Copy command — clipboard.writeText(state.update_command). Lets a
//                  user fall back to running the command in their own
//                  terminal (where they can authenticate sudo, debug
//                  network, etc.).
//   Close        — CLOSE_UPDATE_MODAL only; preserves the failed run
//                  state in case the user reopens to copy the command.

const STDERR_TAIL = 12;

function _formatExitMessage(stream: UpdateStreamEvent[], errorMessage: string | null): string {
  // Find the most recent `exit` event for the rc + the most recent
  // `error_event` for the message.
  for (let i = stream.length - 1; i >= 0; i--) {
    const ev = stream[i];
    if (ev.type === 'exit' && typeof ev.rc === 'number') {
      return `exited rc=${ev.rc}`;
    }
    if (ev.type === 'error_event') {
      return ev.message ?? 'worker error';
    }
  }
  return errorMessage ?? 'unknown failure';
}

export function UpdateFailedModal() {
  const update = useSyncExternalStore(subscribeStore, () => getState().update);
  const [copied, setCopied] = useState<'idle' | 'ok' | 'err'>('idle');
  const cmd = update.state?.update_command ?? '';
  const stderrLines = update.stream
    .filter((e) => e.type === 'stderr' && e.data)
    .map((e) => e.data as string)
    .slice(-STDERR_TAIL);
  const exitMsg = _formatExitMessage(update.stream, update.errorMessage);

  function onRetry() {
    dispatch({ type: 'RESET_UPDATE_RUN' });
    startUpdate();
  }

  async function onCopy() {
    if (!cmd) return;
    try {
      await navigator.clipboard.writeText(cmd);
      setCopied('ok');
      window.setTimeout(() => setCopied('idle'), 1500);
    } catch {
      setCopied('err');
      window.setTimeout(() => setCopied('idle'), 1500);
    }
  }

  return (
    <div className="update-modal-body">
      <div className="update-running-row update-running-row-failed">
        <span className="update-x" aria-hidden="true">
          ✗
        </span>
        <code className="update-cmd update-cmd-inline">{cmd || 'cctally update'}</code>
        <span className="update-elapsed">{exitMsg}</span>
      </div>
      {stderrLines.length > 0 ? (
        <>
          <div className="update-row-label">
            stderr (last {stderrLines.length} line{stderrLines.length === 1 ? '' : 's'})
          </div>
          <pre className="update-stream update-stream-fail">
            {stderrLines.map((l, i) => (
              <span key={i} className="update-stream-line err">
                {l}
                {'\n'}
              </span>
            ))}
          </pre>
        </>
      ) : (
        <p className="update-manual-note">
          {update.errorMessage ?? 'See ~/.local/share/cctally/update.log for the full failure log.'}
        </p>
      )}
      <p className="update-stream-cap">
        Full log: <code>~/.local/share/cctally/update.log</code>
      </p>
      <div className="update-actions">
        <button
          type="button"
          className="update-btn update-btn-primary"
          onClick={onRetry}
        >
          Retry
        </button>
        <button
          type="button"
          className="update-btn"
          onClick={onCopy}
          disabled={!cmd}
        >
          {copied === 'ok'
            ? 'Copied!'
            : copied === 'err'
              ? 'Copy failed'
              : 'Copy command'}
        </button>
        <button
          type="button"
          className="update-btn"
          onClick={() => updateActions.close()}
        >
          Close
        </button>
      </div>
    </div>
  );
}
