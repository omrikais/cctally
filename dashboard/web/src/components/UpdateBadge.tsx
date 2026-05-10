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
  return (
    <button
      type="button"
      className="badge-update"
      title={`Update available · ${cur} → ${latest} · click to view`}
      aria-label={`Update available, current version ${cur}, latest ${latest}`}
      onClick={() => updateActions.open()}
    >
      <span className="badge-update-arrow" aria-hidden="true">↑</span>
      <span className="badge-update-version">v{latest}</span>
    </button>
  );
}
