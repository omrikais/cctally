import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { updateActions } from '../store/update';

const METHOD_LABEL: Record<string, string> = {
  brew: 'Homebrew',
  npm: 'npm',
  unknown: 'unknown',
};

// "Idle" pre-update modal (visual companion State 2). Shows
// Current/Latest/Method/Will-run, optional Release-notes link, and three
// CTAs: Update now / Skip this version / Remind in 7 days.
//
// Manual-fallback variant: when method === 'unknown' the Update button
// is disabled (subprocess can't safely run without a known install
// method) and a copy-command pill replaces the run hint. Spec §2.4.
//
// Prerelease note (spec §1.8): when state.prerelease_note is non-null,
// render it above the actions so prerelease users see the canned
// "manage manually" message before clicking Update.
export function UpdateIdleModal() {
  const update = useSyncExternalStore(subscribeStore, () => getState().update);
  const state = update.state;
  if (!state) return null;
  const method = state.method;
  const methodLabel = METHOD_LABEL[method] ?? method;
  const cmd = state.update_command ?? '';
  const isManual = method === 'unknown' || !cmd;
  return (
    <div className="update-modal-body">
      <div className="update-kv">
        <span className="k">Current</span>
        <span className="v">{state.current_version ?? '—'}</span>
        <span className="k">Latest</span>
        <span className="v">{state.latest_version ?? '—'}</span>
        <span className="k">Method</span>
        <span className="v">
          {methodLabel}
          <span className="muted"> (auto-detected)</span>
        </span>
      </div>
      {!isManual ? (
        <>
          <div className="update-row-label">Will run</div>
          <code className="update-cmd">{cmd}</code>
        </>
      ) : (
        <>
          <div className="update-row-label">Update manually</div>
          <p className="update-manual-note">
            Couldn&apos;t auto-detect this install&apos;s package manager.
            See <a
              href="https://github.com/omrikais/cctally#updating"
              target="_blank"
              rel="noopener noreferrer"
            >Updating</a> for the manual recipe.
          </p>
        </>
      )}
      {state.release_notes_url ? (
        <>
          <div className="update-row-label">Release notes</div>
          <div className="update-link">
            <a href={state.release_notes_url} target="_blank" rel="noopener noreferrer">
              ↗ {state.release_notes_url.replace(/^https?:\/\//, '')}
            </a>
          </div>
        </>
      ) : null}
      {state.prerelease_note ? (
        <div
          className="update-prerelease-note"
          role="note"
          aria-label="Prerelease note"
        >
          {state.prerelease_note}
        </div>
      ) : null}
      <div className="update-actions">
        <button
          type="button"
          className="update-btn update-btn-primary"
          disabled={isManual}
          onClick={() => updateActions.start()}
        >
          Update now
        </button>
        <button
          type="button"
          className="update-btn"
          disabled={!state.latest_version}
          onClick={() => updateActions.skip(state.latest_version)}
        >
          Skip this version
        </button>
        <button
          type="button"
          className="update-btn"
          onClick={() => updateActions.remind(7)}
        >
          Remind in 7 days
        </button>
      </div>
    </div>
  );
}
