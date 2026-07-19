import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import type { PanelGate } from '../lib/sourceGating';
import type { SourceName } from '../types/envelope';

// Small provider-aware primitives shared by the canonical panels. Provider
// adaptation lives in dashboardPresentation.ts; there is deliberately no
// alternate Codex shell or table renderer here.

export function useActiveSource() {
  return useSyncExternalStore(subscribeStore, () => getState().activeSource);
}

export function SourceChip({ source }: { source: SourceName }) {
  return (
    <span className={`source-chip source-chip--${source}`} data-source={source}>
      {source === 'claude' ? 'Claude' : 'Codex'}
    </span>
  );
}

export function DegradedChip({ gate }: { gate: PanelGate }) {
  const detail = gate.noSuccessYet
    ? 'no successful snapshot yet'
    : gate.warning?.message ?? 'degraded';
  const label = gate.noSuccessYet ? 'no snapshot' : 'partial';
  return (
    <span className="panel-degraded-chip" role="status" title={detail} aria-label={`Degraded: ${detail}`}>
      {label}
    </span>
  );
}
