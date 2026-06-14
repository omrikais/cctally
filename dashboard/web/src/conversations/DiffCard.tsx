import { useMemo, useState } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { PencilIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { LineNumberedCode } from './LineNumberedCode';
import { LoadFull } from './LoadFull';
import { highlightBody } from './CodeBlock';
import { fileLangForCall } from './toolLang';
import {
  computeDiff,
  computeWrite,
  computeMultiEdit,
  type DiffRow,
} from './computeDiff';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// One rendered diff body: a set of rows (Edit/Write) is a single hunk; MultiEdit
// is N hunks rendered under `edit k of n` dividers.
type Hunk = DiffRow[];

interface EditInput {
  file_path?: unknown;
  old_string?: unknown;
  new_string?: unknown;
  replace_all?: unknown;
  content?: unknown;
  edits?: unknown;
}

function inputOf(call: Call): EditInput {
  return (call.input as EditInput | null | undefined) ?? {};
}

function filePathOf(inp: EditInput): string {
  return typeof inp.file_path === 'string' ? inp.file_path : '';
}

// Split a path into (parentDir, basename) for the header. A bare filename has no
// parent dir.
function splitPath(path: string): { dir: string; base: string } {
  const slash = path.lastIndexOf('/');
  if (slash < 0) return { dir: '', base: path };
  return { dir: path.slice(0, slash + 1), base: path.slice(slash + 1) };
}

// +N / −M across every hunk (added/removed row counts).
function statOf(hunks: Hunk[]): { add: number; del: number } {
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

// Build the hunks for whichever edit family this is. `override` (the
// load-full-recomputed full input) supersedes the bounded call.input when set.
function buildHunks(call: Call, override: EditInput | null): { hunks: Hunk[]; kind: 'edit' | 'multiedit' | 'write' } {
  const name = (call.name ?? '').toLowerCase();
  const inp = override ?? inputOf(call);
  if (name === 'write') {
    const content = typeof inp.content === 'string' ? inp.content : '';
    return { hunks: [computeWrite(content)], kind: 'write' };
  }
  if (name === 'multiedit') {
    const edits = Array.isArray(inp.edits) ? (inp.edits as { old_string?: unknown; new_string?: unknown }[]) : [];
    return { hunks: computeMultiEdit(edits), kind: 'multiedit' };
  }
  const oldStr = typeof inp.old_string === 'string' ? inp.old_string : '';
  const newStr = typeof inp.new_string === 'string' ? inp.new_string : '';
  return { hunks: [computeDiff(oldStr, newStr)], kind: 'edit' };
}

// One diff row. Context lines route through highlightBody (full syntax color);
// changed lines render the tint + intra-line word-emphasis as PLAIN text (no
// per-token color — spec §4.1 / Codex P2.6). The gutter shows relative old/new
// running numbers (absolute file offsets aren't derivable from old/new strings).
function DiffRowEl({ row, lang }: { row: DiffRow; lang: string }) {
  const sign = row.type === 'add' ? '+' : row.type === 'del' ? '−' : ' ';
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

function HunkEl({ rows, lang }: { rows: Hunk; lang: string }) {
  return (
    <div className="conv-diff-hunk">
      {rows.map((r, i) => (
        <DiffRowEl key={i} row={r} lang={lang} />
      ))}
    </div>
  );
}

// Unified word-diff card for the Edit / MultiEdit / Write family (#177 S3, spec
// §4.1). Collapsed-disclosure chrome mirrors the other Session-2/3 cards
// (conv-chip + chevron + icon + name + preview), so collapse-all [/] and j/k nav
// behave identically. The DiffCard assumes valid input — the input-presence
// guard lives in specialToolRenderer (Codex P1.2), so we never return null.
export function DiffCard({ call }: { call: Call }) {
  // The full input loaded on demand when call.input was truncated (#178);
  // supersedes the bounded call.input for the diff recompute.
  const [fullInput, setFullInput] = useState<EditInput | null>(null);

  const inp = inputOf(call);
  const filePath = filePathOf(inp);
  const { dir, base } = splitPath(filePath);
  const lang = fileLangForCall(call);

  const { hunks, kind } = useMemo(
    () => buildHunks(call, fullInput),
    [call, fullInput],
  );
  // The live stat is counted from the rendered hunks (jsdiff). When the input was
  // truncated AND the full input hasn't been loaded yet, those hunks reflect only
  // the bounded leaves, so the badge would undercount (#198). In that one case
  // prefer the ingest-stamped `edit_stat` (computed from the FULL input) — it
  // matches jsdiff's counts exactly. Once the full input loads (fullInput set) we
  // fall back to the live stat so header==body. Legacy rows lack edit_stat → live.
  const liveStat = useMemo(() => statOf(hunks), [hunks]);
  const stampedStat =
    call.edit_stat &&
    typeof call.edit_stat.add === 'number' &&
    typeof call.edit_stat.del === 'number'
      ? call.edit_stat
      : null;
  const stat =
    call.input_truncated && !fullInput && stampedStat ? stampedStat : liveStat;

  const replaceAll = inp.replace_all === true;
  const editCount = kind === 'multiedit' ? hunks.length : 0;

  // Copy target: the unified diff text (with +/−/space signs), useful to paste.
  const copyText = useMemo(
    () =>
      hunks
        .flatMap((h) =>
          h.map((r) => {
            const s = r.type === 'add' ? '+' : r.type === 'del' ? '-' : ' ';
            return s + r.text;
          }),
        )
        .join('\n'),
    [hunks],
  );

  return (
    <details className="conv-chip conv-chip--tool conv-diff-card" open>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <PencilIcon />
        <span className="conv-chip-name">{call.name ?? 'Edit'}</span>
        <span className="conv-diff-hdr">
          <span className="conv-diff-base">{base || '(file)'}</span>
          {dir && <span className="conv-diff-dir">{dir}</span>}
          {kind === 'write' ? (
            <span className="conv-diff-stat conv-diff-stat--write">wrote {stat.add} lines</span>
          ) : (
            <span className="conv-diff-stat">
              <span className="conv-diff-stat-add">+{stat.add}</span>{' '}
              <span className="conv-diff-stat-del">−{stat.del}</span>
            </span>
          )}
          {replaceAll && <span className="conv-diff-tag">replace all</span>}
          {editCount > 0 && (
            <span className="conv-diff-tag">
              {editCount} edit{editCount === 1 ? '' : 's'}
            </span>
          )}
        </span>
      </summary>
      <div className="conv-diff-body">
        <div className="conv-diff-copy">
          <CopyButton text={copyText} />
        </div>
        {hunks.map((rows, hi) => (
          <div key={hi}>
            {kind === 'multiedit' && (
              <div className="conv-diff-divider">
                edit {hi + 1} of {hunks.length}
              </div>
            )}
            <HunkEl rows={rows} lang={lang} />
          </div>
        ))}
        {call.input_truncated && (
          <LoadFull
            toolUseId={call.tool_use_id ?? ''}
            which="input"
            fullLength={null}
            label="load full input"
            onLoaded={(payload) => {
              if (payload.which === 'input') setFullInput(payload.input as EditInput);
            }}
          />
        )}
        {call.result && (
          <details className="conv-chip conv-chip--result conv-diff-result">
            <summary>
              <span className="conv-chev" aria-hidden="true" />
              <span className="conv-chip-name">result</span>
              <span className="conv-chip-preview">cat -n snippet</span>
            </summary>
            <div className="conv-chip-body conv-tool-io">
              <CopyButton text={call.result.text} />
              <LineNumberedCode code={call.result.text} lang={lang} />
            </div>
          </details>
        )}
      </div>
    </details>
  );
}
