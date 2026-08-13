import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { resolveSourceView } from '../store/sourceView';
import { combinedPresentation, warningForSource } from '../lib/sourceGating';
import type { AllSourceData, SourceEntry } from '../types/envelope';

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
  // #556 S1 §4.4 — on the All selection the combined figure's own state comes
  // from the one authoritative predicate, never from `entry.freshness` (a
  // source-wide axis) and never from warning order (All flattens both
  // providers' warnings with no provenance field).
  const withheld = active === 'all'
    ? combinedPresentation(entry as SourceEntry<AllSourceData>).unavailable
    : null;

  let label: string;
  let detail: string;
  // The withheld reason sits BELOW `degraded && warning`, not above it. It
  // describes one figure; a source-wide warning describes the whole source, and
  // the chip has room for one label. Multi-account decoration withholds the
  // combined figure permanently while leaving All `partial/fresh`, so ranking
  // the withheld reason first pinned "Combined withheld" on every decorated
  // install and masked every other warning behind it for good — a degraded
  // `codex_metadata_incomplete` would have stopped surfacing as "Projects
  // partial". The withheld reason is still reachable in `detail`, which is what
  // the tooltip and the accessible name read, and the hero states it in full.
  if (noSuccessYet) label = 'no successful snapshot yet';
  else if (degraded && warning) label = conciseWarningLabel(warning.domain, warning.code);
  else if (withheld != null) label = 'Combined withheld';
  else if (degraded) label = 'degraded';
  else label = stale ? 'stale' : 'fresh';
  // The detail explains the LABEL, so it follows the same precedence. Pinning
  // it to the withheld reason while the label named a warning would put two
  // different subjects in one chip.
  detail = (degraded && warning ? warning.message : null)
    ?? withheld?.message ?? warning?.message ?? label;
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
  // #556 S1 §4.2 — the `combined_totals_stale` mapping is gone with the code
  // itself. The server can no longer emit it, and the combined figure's state
  // now reaches this chip through `combinedPresentation` above.
  return domain != null ? (WARNING_DOMAIN_LABELS[domain] ?? 'Source degraded') : 'Source degraded';
}

function compactStatusLabel(label: string): string {
  if (label === 'no successful snapshot yet') return 'No snapshot';
  if (label === 'Source degraded' || label === 'degraded') return 'Degraded';
  if (label === 'Projects partial') return 'Projects';
  // #556 S1 §4.5 — the compact form keeps the STATE word. The retired
  // 'Combined stale' -> 'Combined' mapping dropped it, leaving colour as the
  // only carrier of the state at 390px, where both spans are `aria-hidden` and
  // the `title` is unreachable by touch. Dropping the subject instead keeps the
  // chip honest: it sits on the All selection, where the subject is not in
  // doubt. The ten `WARNING_DOMAIN_LABELS` strip their state word through the
  // rule below and are the same defect on a wider surface, filed separately.
  if (label === 'Combined withheld') return 'Withheld';
  return label.replace(/ unavailable$/, '');
}
