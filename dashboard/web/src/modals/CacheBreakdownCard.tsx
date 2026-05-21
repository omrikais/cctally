// CacheBreakdownCard — reusable section-6 sub-card.
//
// Used twice in the Cache Report modal: once as the by-project card
// and once as the by-model card. Rendering differs only in heading
// label + accent color (driven by the ``kind`` prop) and the empty-
// state copy.
//
// 3-column body table: key (project path or model name) · cache % ·
// net $. Rows arrive pre-sorted from the Python builder (by
// abs(net_usd) descending, capped at 5 + ``(other)``). The card is
// presentation-only — no client-side sort.
//
// Spec 2026-05-21 §3.8.
import type { CacheReportBreakdownRow } from '../types/envelope';

export interface CacheBreakdownCardProps {
  kind: 'projects' | 'models';
  rows: CacheReportBreakdownRow[];
}

function fmtSignedUsd(n: number): string {
  // Same convention as the panel + spotlight — Unicode minus on
  // negatives so the column visually distinguishes "saved" from "lost."
  const sign = n >= 0 ? '+' : '−';
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

function floorPct(p: number): number {
  return Math.floor(p + 1e-9);
}

export function CacheBreakdownCard({ kind, rows }: CacheBreakdownCardProps) {
  const cls = kind === 'projects' ? 'bd-projects' : 'bd-models';
  const label = kind === 'projects' ? 'By project' : 'By model';
  const emptyText =
    kind === 'projects'
      ? 'No project activity in this window'
      : 'No model activity in this window';

  return (
    <div className={`crm-bd-card ${cls}`} data-bd-kind={kind}>
      <div className="crm-bd-head">{label}</div>
      {rows.length === 0 ? (
        <div className="empty">{emptyText}</div>
      ) : (
        <table>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} data-testid="crm-bd-row" data-bd-key={r.key}>
                <td>{r.key}</td>
                <td>{floorPct(r.cache_hit_percent)}%</td>
                <td className={r.net_usd >= 0 ? 'net-pos' : 'net-neg'}>
                  {fmtSignedUsd(r.net_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
