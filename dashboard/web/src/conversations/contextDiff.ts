// Pure, JSX-free git-context diff segmenter + unified-diff parser (#217 S5 F6,
// spec §6). Injected `meta_kind:'context'` bodies sometimes carry an UNFENCED
// git diff (e.g. `- Unstaged changes: diff --git a/CLAUDE.md b/CLAUDE.md …`), so
// the reader splits the body into prose + diff regions and renders diff regions
// through the shared DiffCard row primitives. Detection is CONSERVATIVE — it
// anchors only on a real `diff --git a/… b/…` marker (NOT bare +/- prefixes,
// which appear in markdown lists / quoted code — Codex P2-1) so a plain bullet
// list never false-detects a diff.

import type { DiffRow } from './computeDiff';

export interface ContextSegment {
  kind: 'prose' | 'diff';
  text: string;
}

// One parsed file within a diff region. `hunks` reuse the DiffCard/computeDiff
// row shape so UnifiedDiffView can render them via the shared HunkEl primitive.
export interface FileDiff {
  oldPath: string;
  newPath: string;
  hunks: DiffRow[][];
}

// The conservative anchor: a line CONTAINING `diff --git a/… b/…`. Real injected
// context puts a prose lead on the same physical line ("- Unstaged changes: diff
// --git …"), so we match anywhere in the line and flush the pre-marker text as
// prose. The `a/…` / `b/…` shape is required (Codex P2-1) so a bare "diff" word
// in prose can't fire it. Each path is a NON-WHITESPACE run (`\S+`), not a greedy
// `.+`: the segmenter slices the marker line to EOL, so any trailing prose after
// the `b/` path must not bleed into the captured path (#224). The unfenced
// `diff --git a/… b/…` header is inherently ambiguous for space-bearing paths;
// real injected context carries space-free paths, so stopping at whitespace is
// the right tradeoff.
const DIFF_GIT_RE = /diff --git a\/\S+ b\/\S+/;

// Git extended-header line prefixes that belong INSIDE a diff region (so a valid
// diff with mode/rename/copy/index headers isn't split early — Codex P2-1).
const EXTENDED_HEADER_PREFIXES = [
  'old mode ',
  'new mode ',
  'new file mode ',
  'deleted file mode ',
  'rename from ',
  'rename to ',
  'copy from ',
  'copy to ',
  'similarity index ',
  'dissimilarity index ',
  'index ',
];

// Is this line part of a diff body/header (given we are already inside a region
// anchored by a `diff --git` marker)?
function isDiffLine(line: string): boolean {
  if (DIFF_GIT_RE.test(line)) return true; // a following file in a multi-file diff
  if (line.startsWith('--- ') || line.startsWith('+++ ')) return true;
  if (line.startsWith('@@')) return true;
  if (EXTENDED_HEADER_PREFIXES.some((p) => line.startsWith(p))) return true;
  // Body lines: context (' '), add ('+'), del ('-'), "\ No newline" ('\').
  // An empty line within a diff is a context line (' ' collapsed to '').
  if (line === '') return true;
  const c = line[0];
  return c === '+' || c === '-' || c === ' ' || c === '\\';
}

export function segmentContextBody(text: string): ContextSegment[] {
  const lines = text.split('\n');
  // A trailing newline yields a final empty element — drop it so it doesn't
  // produce a phantom trailing prose segment.
  if (lines.length && lines[lines.length - 1] === '') lines.pop();

  const segments: ContextSegment[] = [];
  let prose: string[] = [];
  let diff: string[] = [];
  let inDiff = false;

  const flushProse = () => {
    if (prose.length) {
      segments.push({ kind: 'prose', text: prose.join('\n') });
      prose = [];
    }
  };
  const flushDiff = () => {
    if (diff.length) {
      segments.push({ kind: 'diff', text: diff.join('\n') });
      diff = [];
    }
  };

  for (const line of lines) {
    if (!inDiff) {
      const m = DIFF_GIT_RE.exec(line);
      if (m && m.index !== undefined) {
        // Flush any pre-marker text on this line as prose, then start the diff
        // region at the marker itself (real context puts a lead before it).
        const pre = line.slice(0, m.index).replace(/\s+$/, '');
        if (pre) prose.push(pre);
        flushProse();
        inDiff = true;
        diff.push(line.slice(m.index));
      } else {
        prose.push(line);
      }
    } else {
      if (isDiffLine(line)) {
        diff.push(line);
      } else {
        // First non-diff line ends the region; resume prose.
        flushDiff();
        inDiff = false;
        prose.push(line);
      }
    }
  }
  flushProse();
  flushDiff();
  return segments;
}

// Split a hunk body value into lines (no trailing-empty phantom).
function pushBodyRows(
  body: string[],
  oldStart: number,
  newStart: number,
): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldNo = oldStart;
  let newNo = newStart;
  for (const raw of body) {
    // A fully-empty line is the trailing-newline split artifact (real context
    // lines carry a leading space), not a content row — skip it.
    if (raw === '') continue;
    const sign = raw[0];
    const txt = raw.slice(1);
    if (sign === '+') {
      rows.push({ type: 'add', oldNo: null, newNo: newNo++, text: txt });
    } else if (sign === '-') {
      rows.push({ type: 'del', oldNo: oldNo++, newNo: null, text: txt });
    } else if (sign === '\\') {
      // "\ No newline at end of file" — not a content row; skip.
      continue;
    } else {
      // context line (leading space, or a fully-empty line)
      rows.push({ type: 'context', oldNo: oldNo++, newNo: newNo++, text: txt });
    }
  }
  return rows;
}

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;
// Mirrors DIFF_GIT_RE: capture each path as a non-whitespace run so trailing
// prose on the marker line can't over-capture newPath (#224). No `$` anchor —
// the path ends at the first whitespace, not end-of-line.
const PATH_RE = /diff --git a\/(\S+) b\/(\S+)/;

// Convert a diff region (one or more `diff --git` files) into per-file
// `{ oldPath, newPath, hunks }`. Line-level only (no intra-line word emphasis —
// the git context carries no old/new pairing the way an Edit does).
export function parseUnifiedDiff(text: string): FileDiff[] {
  const lines = text.split('\n');
  const files: FileDiff[] = [];
  let cur: FileDiff | null = null;
  let hunkBody: string[] = [];
  let hunkOld = 1;
  let hunkNew = 1;
  let inHunk = false;

  const flushHunk = () => {
    if (inHunk && cur) {
      cur.hunks.push(pushBodyRows(hunkBody, hunkOld, hunkNew));
    }
    hunkBody = [];
    inHunk = false;
  };

  for (const line of lines) {
    const pm = PATH_RE.exec(line);
    if (pm) {
      flushHunk();
      cur = { oldPath: pm[1], newPath: pm[2], hunks: [] };
      files.push(cur);
      continue;
    }
    const hm = HUNK_RE.exec(line);
    if (hm) {
      flushHunk();
      hunkOld = parseInt(hm[1], 10);
      hunkNew = parseInt(hm[2], 10);
      inHunk = true;
      continue;
    }
    if (inHunk) {
      // Stop accumulating body at a non-body header (---/+++/extended).
      if (
        line.startsWith('--- ') ||
        line.startsWith('+++ ') ||
        EXTENDED_HEADER_PREFIXES.some((p) => line.startsWith(p))
      ) {
        // headers between hunks/files — ignore (they are not body rows).
        continue;
      }
      hunkBody.push(line);
    }
  }
  flushHunk();
  return files;
}
