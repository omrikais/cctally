import { useSyncExternalStore, type ReactNode } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import type { SourceResource } from '../hooks/useSourceDetail';
import { useSnapshot } from '../hooks/useSnapshot';
import { resolveSourceView } from '../store/sourceView';
import {
  gatePanel,
  providerSections,
  resolvePanelData,
  type PanelGate,
} from '../lib/sourceGating';
import { fmt } from '../lib/fmt';
import type { PanelId } from '../lib/panelIds';
import type {
  CodexPeriodView,
  CodexProjectsDomain,
  CodexQuotaDomain,
  SourceName,
} from '../types/envelope';

// #294 S5 §6.2-§6.5 — the source-aware panel shell. Panels keep their Claude
// implementation verbatim (the seam resolves Claude value-identical to the
// legacy fields); this shell adds Codex-native rendering and All provider-
// labeled sections, plus the §5.5-Layer-3 skeleton/degraded chrome. Panels are
// only MOUNTED when visible (App filters the grid through the visible-panel
// order), so the shell never needs to self-hide in single-source mode.

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
  const label = gate.noSuccessYet
    ? 'no successful snapshot yet'
    : gate.warning?.message ?? 'degraded';
  return (
    <span className="panel-degraded-chip" role="status" aria-label={`Degraded: ${label}`}>
      {label}
    </span>
  );
}

function PanelSkeletonBlock() {
  return <div className="panel-source-skeleton" data-testid="panel-source-skeleton" aria-hidden="true" />;
}

function PanelEmpty({ label }: { label: string }) {
  return <div className="panel-source-empty" data-testid="panel-source-empty">{label}</div>;
}

export interface SourcePanelShellProps {
  panel: PanelId;
  panelKind: string;
  claude: ReactNode;
  // Renders the Codex-native content from the panel's mapped source-data path.
  codex: (pathData: unknown) => ReactNode;
  emptyLabel?: string;
}

// The shell branches on the active source. Claude renders unchanged; Codex
// renders the native path with skeleton/degraded chrome; All renders provider-
// labeled sections (Claude section is the legacy panel; Codex section is the
// native render) — no cross-source merge.
export function SourcePanelShell({
  panel,
  panelKind,
  claude,
  codex,
  emptyLabel = 'No activity yet.',
}: SourcePanelShellProps) {
  const activeSource = useActiveSource();
  const env = useSnapshot();
  const view = resolveSourceView(env, activeSource);

  if (activeSource === 'claude') return <>{claude}</>;

  if (activeSource === 'codex') {
    const gate = gatePanel(view, panel);
    // A panel the source can never have (no data path — e.g. Codex
    // forecast/trend/cache-report) renders nothing. Defense-in-depth: the grid
    // already unmounts hidden panels via the visible-panel order, so this only
    // fires when a panel is mounted directly (a test, or a future caller).
    if (gate.mode === 'hidden') return null;
    if (gate.mode === 'skeleton') return <PanelSkeletonBlock />;
    const data = resolvePanelData(view, panel);
    if (data == null) return <PanelEmpty label={emptyLabel} />;
    return (
      <div className="panel-source-codex" data-panel-kind={panelKind} data-source="codex">
        {gate.mode === 'degraded' && <DegradedChip gate={gate} />}
        {codex(data)}
      </div>
    );
  }

  // All — provider-labeled sections (§5.5 Layer 2).
  const sections = providerSections(view, panel);
  const visible = sections.filter((s) => s.gate.mode !== 'hidden');
  if (visible.length === 0) {
    // Both providers hydrating → skeleton; else honest empty.
    const anySkeleton = sections.some((s) => s.gate.mode === 'skeleton');
    return anySkeleton ? <PanelSkeletonBlock /> : <PanelEmpty label={emptyLabel} />;
  }
  return (
    <div className="source-all-sections" data-panel-kind={panelKind} data-source="all">
      {visible.map((sec) => (
        <section className="source-provider-section" key={sec.source} data-source={sec.source}>
          <div className="source-provider-head">
            <SourceChip source={sec.source} />
            {sec.gate.mode === 'degraded' && <DegradedChip gate={sec.gate} />}
          </div>
          {sec.gate.mode === 'skeleton' ? (
            <PanelSkeletonBlock />
          ) : sec.source === 'claude' ? (
            claude
          ) : (
            codex(sec.data)
          )}
        </section>
      ))}
    </div>
  );
}

// ---- Concise Codex-native renderers (provider vocabulary) -------------

export function CodexPeriodTable({ data, label }: { data: unknown; label: string }) {
  const view = data as CodexPeriodView | null;
  const rows = view?.rows ?? [];
  return (
    <table className="codex-period-table" data-testid={`codex-period-${label.toLowerCase()}`}>
      <caption className="sr-only">{label} (Codex)</caption>
      <thead>
        <tr>
          <th scope="col">{label}</th>
          <th scope="col">Cost</th>
          <th scope="col">Tokens</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr><td colSpan={3}>No Codex activity.</td></tr>
        ) : (
          rows.map((r) => (
            <tr key={r.label}>
              <td>{r.label}</td>
              <td>{fmt.usd2(r.cost_usd)}</td>
              <td>{fmt.tokens(r.total_tokens)}</td>
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}

// A first-cell button that opens the qualified detail modal for a Codex row.
// (A role="button" on the <tr> with nested cells would be invalid ARIA — the
// interactive control is a real cell button.)
function CodexDetailButton({
  resource,
  detailKey,
  label,
}: {
  resource: SourceResource;
  detailKey: string;
  label: string;
}) {
  return (
    <button
      type="button"
      className="codex-detail-open"
      onClick={() =>
        dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource, key: detailKey })
      }
    >
      {label}
    </button>
  );
}

export function CodexProjectsTable({ data }: { data: unknown }) {
  const domain = data as CodexProjectsDomain | null;
  const rows = domain?.rows ?? [];
  return (
    <table className="codex-projects-table" data-testid="codex-projects-table">
      <thead>
        <tr>
          <th scope="col">Project</th>
          <th scope="col">Sessions</th>
          <th scope="col">Cost</th>
          <th scope="col">Tokens</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr><td colSpan={4}>No Codex projects.</td></tr>
        ) : (
          rows.map((r) => (
            <tr key={r.key}>
              <td><CodexDetailButton resource="project" detailKey={r.key} label={r.label} /></td>
              <td>{r.session_count}</td>
              <td>{fmt.usd2(r.cost_usd)}</td>
              <td>{fmt.tokens(r.total_tokens)}</td>
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}

export function CodexBlocksList({ data }: { data: unknown }) {
  const quota = data as CodexQuotaDomain | null;
  const blocks = quota?.blocks ?? [];
  return (
    <ul className="codex-blocks-list" data-testid="codex-blocks-list">
      {blocks.length === 0 ? (
        <li>No Codex quota windows.</li>
      ) : (
        blocks.map((b) => (
          <li key={b.key} data-orphaned={b.orphaned || undefined}>
            <CodexDetailButton resource="block" detailKey={b.key} label={b.label} />
            <span className="cbl-pct">{fmt.pct0(b.current_percent)}</span>
          </li>
        ))
      )}
    </ul>
  );
}

// #294 S5 §6.3 — the Sessions surface is rendered by the source-aware
// `SourceSessionsGrid` (full panel chrome + roving grid + sort/filter/search),
// NOT by a static shell table. See panels/SourceSessionsGrid.tsx.
