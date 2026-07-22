import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { updateActions } from '../store/update';

// Amber update chip. Renders only when `update.state.available` is true
// — the cooking happens in `store/update.ts#refreshUpdateState` (server
// returns raw state + suppress; the predicate matches the Python
// `_format_update_check_json`). Click → opens the update modal.
//
// Hover tooltip carries `current → latest` so a desktop user gets the
// version delta without opening the modal. The button also exposes a
// version-string-equivalent label to assistive tech.
export function UpdateBadge() {
  const update = useSyncExternalStore(subscribeStore, () => getState().update);
  const state = update.state;
  if (!state || !state.available || !state.latest_version) return null;
  const cur = state.current_version ?? '?';
  const latest = state.latest_version;
  // Beta-channel (spec 2026-07-21 §3): a `(beta)` marker when the configured
  // release channel is beta. The install command carried in state.update_command
  // is already the channel-correct exact-version form.
  const isBeta = state.configured_channel === 'beta';
  const channelSuffix = isBeta ? ' (beta)' : '';
  return (
    <button
      type="button"
      className="badge-update"
      title={`Update available · ${cur} → ${latest}${channelSuffix} · click to view`}
      aria-label={`Update available${isBeta ? ' (beta channel)' : ''}, current version ${cur}, latest ${latest}`}
      onClick={() => updateActions.open()}
    >
      <span className="badge-update-arrow" aria-hidden="true">↑</span>
      <span className="badge-update-version">v{latest}</span>
      {isBeta && <span className="badge-update-channel">beta</span>}
    </button>
  );
}
