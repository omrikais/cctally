import { useMemo, useState } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { PencilIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { LineNumberedCode } from './LineNumberedCode';
import { LoadFull } from './LoadFull';
import { fileLangForCall } from './toolLang';
import { HunkEl, type Hunk } from './diffPrimitives';
import {
  computeDiff,
  computeWrite,
  computeMultiEdit,
} from './computeDiff';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

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

// #217 S3 E10#5 — derive the result-disclosure preview from the actual snippet.
// The Edit/Write/MultiEdit result is a `cat -n` numbered view of the file after
// the change; "N lines" names its size, which is far more useful than a fixed
// "cat -n snippet" string. An empty result degrades to a bare "result" hint.
function resultPreview(text: string): string {
  const trimmed = text.replace(/\n+$/, '');
  if (trimmed.length === 0) return 'snippet';
  const n = trimmed.split('\n').length;
  return `${n} line${n === 1 ? '' : 's'}`;
}

// Split a path into (parentDir, basename) for the header. A bare filename has no
// parent dir.
function splitPath(path: string): { dir: string; base: string } {
  const slash = path.lastIndexOf('/');
  if (slash < 0) return { dir: '', base: path };
  return { dir: path.slice(0, slash + 1), base: path.slice(slash + 1) };
}

// #217 S5 §4 / I-1.4 — build a REAL unified diff (`diff --git`/`---`/`+++`/`@@`)
// from the rendered hunks + file_path (Codex P1-6). A Write yields an applyable
// add-patch against /dev/null; Edit/MultiEdit hunk line numbers are
// snippet-relative (not whole-file offsets), so those patches are a best-effort
// shareable representation — they may not cleanly `git apply` (documented in
// docs/commands/dashboard.md, not the UI).
function hunkHeader(rows: Hunk): string {
  // old/new start = the first row carrying that side's number (snippet-relative);
  // length = count of rows touching that side (context counts for both).
  let oldStart: number | null = null;
  let newStart: number | null = null;
  let oldLen = 0;
  let newLen = 0;
  for (const r of rows) {
    if (r.type !== 'add') {
      if (oldStart === null && r.oldNo != null) oldStart = r.oldNo;
      oldLen++;
    }
    if (r.type !== 'del') {
      if (newStart === null && r.newNo != null) newStart = r.newNo;
      newLen++;
    }
  }
  return `@@ -${oldStart ?? (oldLen ? 1 : 0)},${oldLen} +${newStart ?? (newLen ? 1 : 0)},${newLen} @@`;
}

function toPatch(filePath: string, hunks: Hunk[], isWrite: boolean): string {
  const path = filePath || 'file';
  const aPath = isWrite ? '/dev/null' : `a/${path}`;
  const bPath = `b/${path}`;
  const lines: string[] = [
    `diff --git a/${path} b/${path}`,
    `--- ${aPath}`,
    `+++ ${bPath}`,
  ];
  for (const rows of hunks) {
    if (rows.length === 0) continue;
    lines.push(hunkHeader(rows));
    for (const r of rows) {
      const sign = r.type === 'add' ? '+' : r.type === 'del' ? '-' : ' ';
      lines.push(sign + r.text);
    }
  }
  return lines.join('\n') + '\n';
}

function basename(path: string): string {
  const slash = path.lastIndexOf('/');
  return slash < 0 ? path : path.slice(slash + 1);
}

function downloadPatch(filePath: string, hunks: Hunk[], isWrite: boolean): void {
  const text = toPatch(filePath, hunks, isWrite);
  const blob = new Blob([text], { type: 'text/x-patch;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${basename(filePath) || 'patch'}.patch`;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 0);
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

// DiffRowEl / HunkEl moved to ./diffPrimitives (shared with UnifiedDiffView,
// #217 S5 F6). The git-context diff renders byte-identical rows via the same
// primitives.

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
          {/* #217 S5 §4 — download a real unified .patch built from the hunks +
              file_path. Snippet-relative for Edit/MultiEdit (best-effort). */}
          <button
            type="button"
            className="conv-diff-patch-btn"
            aria-label="Download .patch"
            title="Download .patch"
            onClick={(e) => {
              e.stopPropagation();
              downloadPatch(filePath, hunks, kind === 'write');
            }}
          >
            .patch
          </button>
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
              {/* #217 S3 E10#5 — derive the disclosure label from the ACTUAL
                  result snippet (its line count) instead of the hardcoded
                  "cat -n snippet" string. The result is the post-edit cat -n
                  view; naming its size is the useful, accurate preview. */}
              <span className="conv-chip-preview">{resultPreview(call.result.text)}</span>
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
