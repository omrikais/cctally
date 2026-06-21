// Shared diff row primitives (#217 S5 F6, spec §6) — extracted verbatim from
// DiffCard.tsx so BOTH the edit-diff card (DiffCard) and the git-context diff
// (UnifiedDiffView) render byte-identical rows. Behavior-preserving extraction:
// DiffCard.test.tsx (incl. the I-1 `.patch` test) must stay green.

import type { DiffRow } from './computeDiff';
import { highlightBody } from './CodeBlock';

// One rendered diff body: a set of rows (Edit/Write) is a single hunk; MultiEdit
// is N hunks rendered under `edit k of n` dividers; a git-context diff is one
// hunk per `@@` block.
export type Hunk = DiffRow[];

// Re-export so consumers get the highlighter from one diff-primitive surface.
export { highlightBody };

// One diff row. Context lines route through highlightBody (full syntax color);
// changed lines render the tint + intra-line word-emphasis as PLAIN text (no
// per-token color — spec §4.1 / Codex P2.6). The gutter shows relative old/new
// running numbers (absolute file offsets aren't derivable from old/new strings).
export function DiffRowEl({ row, lang }: { row: DiffRow; lang: string }) {
  const sign = row.type === 'add' ? '+' : row.type === 'del' ? '−' : ' ';
  let content: React.ReactNode;
  if (row.type === 'context') {
    // Full syntax highlighting on unchanged lines.
    content = highlightBody(row.text, lang);
  } else if (row.segments) {
    // Changed line with word-diff: brighten the emphasized segments, plain text.
    content = row.segments.map((s, i) =>
      s.emph ? (
        <span key={i} className="conv-diff-word">
          {s.text}
        </span>
      ) : (
        <span key={i}>{s.text}</span>
      ),
    );
  } else {
    // Changed line with no word pairing (unpaired add/del) — plain text.
    content = row.text;
  }
  return (
    <div className={`conv-diff-row conv-diff-row--${row.type}`}>
      <span className="conv-diff-gutter" aria-hidden="true">
        {row.oldNo ?? ''}
      </span>
      <span className="conv-diff-gutter" aria-hidden="true">
        {row.newNo ?? ''}
      </span>
      <span className="conv-diff-sign" aria-hidden="true">
        {sign}
      </span>
      <span className="conv-diff-text">{content}</span>
    </div>
  );
}

export function HunkEl({ rows, lang }: { rows: Hunk; lang: string }) {
  return (
    <div className="conv-diff-hunk">
      {rows.map((r, i) => (
        <DiffRowEl key={i} row={r} lang={lang} />
      ))}
    </div>
  );
}
