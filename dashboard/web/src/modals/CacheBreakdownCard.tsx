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
// Long project paths are middle-truncated so a card at ~310 px wide
// keeps both the leading segment (so the user can tell which volume /
// repo) and the basename (so they can tell which checkout / worktree).
// The full path is preserved in the `title` attribute for hover-to-reveal.
//
// Spec 2026-05-21 §3.8.
import type { CacheReportBreakdownRow } from '../types/envelope';
import { fmt } from '../lib/fmt';

export interface CacheBreakdownCardProps {
  kind: 'projects' | 'models';
  rows: CacheReportBreakdownRow[];
}

// Middle-truncate a filesystem path so a 2-column breakdown card
// (~310 px wide on a 720 px modal, with ~180 px available for the key
// cell after the cache-% and net columns) keeps the leading segment
// AND the trailing 1-2 segments visible. Returns the original path
// when no truncation is needed.
//
// The default budget (28 chars) is sized to monospace 12 px (≈6.5 px
// per char) so JS middle-truncation absorbs every realistic project
// path BEFORE the CSS nowrap-ellipsis fallback in .bd-key kicks in —
// that fallback only clips from the right and loses the basename,
// which is exactly the segment a user needs to disambiguate
// worktrees / checkouts.
//
// Examples (maxLen = 28):
//   /Volumes/TRANSCEND/repos/cctally-dev
//     -> /Volumes/…/repos/cctally-dev      (36 -> 28 chars; cand-1 fits)
//   /Volumes/TRANSCEND/.../feat/projects-panel
//     -> /Volumes/…/projects-panel         (cand-2 once cand-1 too long)
//   /Volumes/.../feature/view-model-unification
//     -> …/view-model-unification          (fallback once both fail)
//   /repos/cctally-dev
//     -> /repos/cctally-dev                (under limit; unchanged)
function shortenPath(path: string, maxLen: number = 28): string {
  if (path.length <= maxLen) return path;
  const parts = path.split('/').filter(Boolean);
  if (parts.length <= 2) return path;
  const lead = '/' + parts[0];
  // Try lead + last two segments first; fall back to last one if still
  // too long.
  let candidate = `${lead}/…/${parts.slice(-2).join('/')}`;
  if (candidate.length <= maxLen) return candidate;
  candidate = `${lead}/…/${parts[parts.length - 1]}`;
  if (candidate.length <= maxLen) return candidate;
  return `…/${parts[parts.length - 1]}`;
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
              const display = isProjects ? shortenPath(r.key) : r.key;
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
