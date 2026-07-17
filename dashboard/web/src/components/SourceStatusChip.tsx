import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { resolveSourceView } from '../store/sourceView';

// #294 S5 — the active-source status chip (§6.8). Distinct from the global
// `SyncChip` (which keeps its sync/disconnect/error meaning untouched): this
// surfaces the ACTIVE source's freshness / last-success (or "no successful
// snapshot yet"), and a degraded source's warning per D2. Dashboard workspace
// only. Renders nothing while hydrating or for a pre-S4 (no-entry) view — there
// is no honest per-source status to show yet.

export function SourceStatusChip() {
  const active = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  const env = useSnapshot();
  if (view !== 'dashboard') return null;

  const sview = resolveSourceView(env, active);
  const entry = sview.entry;
  if (sview.hydrating || entry == null) return null;

  const noSuccessYet = entry.last_success_at == null;
  const degraded = entry.availability === 'partial' || entry.availability === 'unavailable';
  const warning = entry.warnings != null && entry.warnings.length > 0 ? entry.warnings[0] : null;
  const stale = entry.freshness === 'stale';

  let label: string;
  if (noSuccessYet) label = 'no successful snapshot yet';
  else if (degraded && warning) label = warning.message;
  else if (degraded) label = 'degraded';
  else label = stale ? 'stale' : 'fresh';

  const cls =
    'source-status-chip' +
    (degraded ? ' is-degraded' : '') +
    (stale && !degraded ? ' is-stale' : '');

  return (
    <span
      className={cls}
      data-testid="source-status-chip"
      data-source={active}
      title={label}
      aria-label={`${active} source status: ${label}`}
    >
      {label}
    </span>
  );
}
