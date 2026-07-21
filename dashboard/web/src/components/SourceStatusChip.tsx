import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { resolveSourceView } from '../store/sourceView';
import { warningForSource } from '../lib/sourceGating';

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
  const warning = warningForSource(entry.warnings);
  const stale = entry.freshness === 'stale';

  let label: string;
  let detail: string;
  if (noSuccessYet) label = 'no successful snapshot yet';
  else if (degraded && warning) label = conciseWarningLabel(warning.domain, warning.code);
  else if (degraded) label = 'degraded';
  else label = stale ? 'stale' : 'fresh';
  detail = warning?.message ?? label;
  const compactLabel = compactStatusLabel(label);

  const cls =
    'source-status-chip' +
    (degraded ? ' is-degraded' : '') +
    (stale && !degraded ? ' is-stale' : '');

  return (
    <span
      className={cls}
      data-testid="source-status-chip"
      data-source={active}
      title={detail}
      aria-label={`${active} source status: ${detail}`}
    >
      <span className="source-status-label source-status-label--full" aria-hidden="true">{label}</span>
      <span className="source-status-label source-status-label--compact" aria-hidden="true">{compactLabel}</span>
    </span>
  );
}

const WARNING_DOMAIN_LABELS: Record<string, string> = {
  hero: 'Hero unavailable',
  daily: 'Daily unavailable',
  weekly: 'Weekly unavailable',
  monthly: 'Monthly unavailable',
  sessions: 'Sessions unavailable',
  projects: 'Projects unavailable',
  quota: 'Quota unavailable',
  budget: 'Budget unavailable',
  forensics: 'Forensics unavailable',
  alerts: 'Alerts unavailable',
};

function conciseWarningLabel(domain: string | undefined, code?: string): string {
  if (domain === 'projects' && code === 'codex_metadata_incomplete') return 'Projects partial';
  return domain != null ? (WARNING_DOMAIN_LABELS[domain] ?? 'Source degraded') : 'Source degraded';
}

function compactStatusLabel(label: string): string {
  if (label === 'no successful snapshot yet') return 'No snapshot';
  if (label === 'Source degraded' || label === 'degraded') return 'Degraded';
  if (label === 'Projects partial') return 'Projects';
  return label.replace(/ unavailable$/, '');
}
