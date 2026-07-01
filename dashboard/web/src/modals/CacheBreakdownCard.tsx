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
// Long project paths render as their basename ("cctally-dev") with the
// full path preserved in the `title` attribute for hover-to-reveal
// (#251 CR-4). This replaces the earlier middle-truncation, whose
// row-to-row output was inconsistent (short paths kept a lead segment
// then CSS-clipped the basename; long paths fell back to "…/basename").
//
// Spec 2026-05-21 §3.8; basename change: #251 CR-4.
import type { CacheReportBreakdownRow } from '../types/envelope';
import { fmt } from '../lib/fmt';

export interface CacheBreakdownCardProps {
  kind: 'projects' | 'models';
  rows: CacheReportBreakdownRow[];
}

// Basename of a filesystem path — the last non-empty segment. CR-4:
// the By-project card shows basenames ("cctally-dev") with the full
// path preserved in the `title` attribute for hover-to-reveal, instead
// of the inconsistent middle-truncation. Falls back to the original
// string when there are no segments.
function basename(path: string): string {
  const parts = path.split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

export function CacheBreakdownCard({ kind, rows }: CacheBreakdownCardProps) {
  const cls = kind === 'projects' ? 'bd-projects' : 'bd-models';
  const label = kind === 'projects' ? 'By project' : 'By model';
  const emptyText =
    kind === 'projects'
      ? 'No project activity in this window'
      : 'No model activity in this window';
  const isProjects = kind === 'projects';

  return (
    <div className={`crm-bd-card ${cls}`} data-bd-kind={kind}>
      <div className="crm-bd-head">{label}</div>
      {rows.length === 0 ? (
        <div className="empty">{emptyText}</div>
      ) : (
        <table>
          <tbody>
            {rows.map((r) => {
              const display = isProjects ? basename(r.key) : r.key;
              const isTruncated = display !== r.key;
              return (
                <tr key={r.key} data-testid="crm-bd-row" data-bd-key={r.key}>
                  <td
                    className="bd-key"
                    title={isTruncated ? r.key : undefined}
                  >
                    {display}
                  </td>
                  <td>{fmt.pctFloor(r.cache_hit_percent)}%</td>
                  <td className={r.net_usd >= 0 ? 'net-pos' : 'net-neg'}>
                    {fmt.usdSigned(r.net_usd)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
