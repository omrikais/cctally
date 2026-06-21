// Renders a parsed git-context diff (#217 S5 F6, spec §6) using the SAME row
// primitives as DiffCard (diffPrimitives.HunkEl) so an injected git diff looks
// byte-identical to an edit diff. Per file: a path header (basename prominent +
// dir muted) + a `+N −M` stat + the hunks. Wired ONLY into MessageItem's
// `meta_kind === 'context'` path via ContextBody.

import type { FileDiff } from './contextDiff';
import { HunkEl } from './diffPrimitives';
import { langFromExtension } from './toolLang';

function splitPath(path: string): { dir: string; base: string } {
  const slash = path.lastIndexOf('/');
  if (slash < 0) return { dir: '', base: path };
  return { dir: path.slice(0, slash + 1), base: path.slice(slash + 1) };
}

// +N / −M across every hunk of one file (added/removed row counts).
function statOf(hunks: FileDiff['hunks']): { add: number; del: number } {
  let add = 0;
  let del = 0;
  for (const h of hunks) {
    for (const r of h) {
      if (r.type === 'add') add++;
      else if (r.type === 'del') del++;
    }
  }
  return { add, del };
}

export function UnifiedDiffView({ files }: { files: FileDiff[] }) {
  if (files.length === 0) return null;
  return (
    <div className="conv-ctx-diff">
      {files.map((file, fi) => {
        // newPath is the post-change path; for a deletion it may be /dev/null —
        // prefer a real path for the header.
        const path =
          file.newPath && file.newPath !== '/dev/null' ? file.newPath : file.oldPath;
        const { dir, base } = splitPath(path);
        const lang = langFromExtension(path);
        const stat = statOf(file.hunks);
        return (
          <div className="conv-ctx-diff-file" key={fi}>
            <div className="conv-diff-hdr">
              <span className="conv-diff-base">{base || '(file)'}</span>
              {dir && <span className="conv-diff-dir">{dir}</span>}
              <span className="conv-diff-stat">
                <span className="conv-diff-stat-add">+{stat.add}</span>{' '}
                <span className="conv-diff-stat-del">−{stat.del}</span>
              </span>
            </div>
            {file.hunks.map((rows, hi) => (
              <HunkEl key={hi} rows={rows} lang={lang} />
            ))}
          </div>
        );
      })}
    </div>
  );
}
