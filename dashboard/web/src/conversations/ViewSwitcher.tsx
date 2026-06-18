import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { transcriptsEnabled } from '../lib/transcripts';

// Header segmented Dashboard｜Conversations control (spec §4 entry).
// Hidden entirely when transcripts are not enabled for this request.
export function ViewSwitcher() {
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  const env = useSnapshot();
  if (!transcriptsEnabled(env)) return null;
  return (
    <div className="view-switcher" role="group" aria-label="Workspace">
      <button
        type="button" aria-pressed={view === 'dashboard'}
        className={`view-seg${view === 'dashboard' ? ' is-active' : ''}`}
        onClick={() => dispatch({ type: 'SET_VIEW', view: 'dashboard' })}
      >Dashboard</button>
      <button
        type="button" aria-pressed={view === 'conversations'}
        className={`view-seg${view === 'conversations' ? ' is-active' : ''}`}
        onClick={() => dispatch({ type: 'SET_VIEW', view: 'conversations' })}
      >Conversations</button>
    </div>
  );
}
