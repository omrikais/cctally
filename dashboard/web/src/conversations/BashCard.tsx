import { useState } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { TerminalIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { AnsiText } from './parseAnsi';
import { LoadFull } from './LoadFull';
import { highlightBody } from './CodeBlock';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

function commandOf(call: Call): string {
  const c = (call.input as { command?: unknown } | null | undefined)?.command;
  return typeof c === 'string' ? c : '';
}

// #193: the Bash tool's own `input.description` (Claude Code writes a short
// human gloss alongside the command). Trimmed-or-undefined (Codex P2-2): a
// blank/whitespace description must fall through to the command preview, never
// render an empty dimmed chip line. Old rows without stored input also fall
// through (the `?.` chain yields undefined).
function descriptionOf(call: Call): string | undefined {
  const d = (call.input as { description?: unknown } | null | undefined)?.description;
  const s = typeof d === 'string' ? d.trim() : '';
  return s.length > 0 ? s : undefined;
}

// Split the merged result text into (stdout, stderr) when call.stderr is a
// suffix of result.text — the empirical storage shape (result.text == stdout +
// stderr, spec §2 / §4.2). When stderr is absent (legacy rows) or not a suffix
// (rare truncated / format-change case), return the whole text as stdout and no
// stderr (the guarded fallback degrades to a merged terminal, never mis-splits).
function splitStreams(text: string, stderr: string | null | undefined): { stdout: string; stderr: string | null } {
  if (typeof stderr === 'string' && stderr.length > 0 && text.endsWith(stderr)) {
    return { stdout: text.slice(0, text.length - stderr.length), stderr };
  }
  return { stdout: text, stderr: null };
}

// Bash tool call rendered as a terminal (#177 S3, spec §4.2): `$ <command>`
// (bash-highlighted) over the output, with stderr split into a red block when
// present and an error/interrupted status badge. Request-only calls (no folded
// result) show the command alone. Chrome mirrors the other cards (conv-chip +
// chevron + icon + name + preview) so collapse-all [/] and j/k nav match. The
// card assumes a valid command input — the presence guard lives in
// specialToolRenderer (Codex P1.2).
export function BashCard({ call }: { call: Call }) {
  // The full output loaded on demand when result.truncated (#178); supersedes
  // the bounded result.text for rendering. The load-full result payload carries
  // a discrete `stderr` field, so we capture it alongside `text` and re-split
  // exactly like the un-truncated path — the red stderr block must survive a
  // load-full (it's precisely when the user asked to see MORE).
  const [full, setFull] = useState<{ text: string; stderr: string | null | undefined } | null>(null);

  const command = commandOf(call);
  const result = call.result;
  const isError = result?.is_error === true;
  const interrupted = call.interrupted === true;

  // After load-full, re-split the LOADED text against the LOADED stderr (same
  // splitStreams contract as the bounded path); before, split the capped
  // result.text against the block-level call.stderr.
  const rawText = full?.text ?? result?.text ?? '';
  const { stdout, stderr } = full == null
    ? splitStreams(rawText, call.stderr)
    : splitStreams(rawText, full.stderr);

  const badge = isError ? (
    <span className="conv-term-badge conv-term-badge--err">● error</span>
  ) : interrupted ? (
    <span className="conv-term-badge conv-term-badge--int">■ interrupted</span>
  ) : null;

  return (
    <details className="conv-chip conv-chip--tool conv-term" open>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <TerminalIcon />
        <span className="conv-chip-name">Bash</span>
        <span className="conv-chip-preview">{descriptionOf(call) ?? call.preview}</span>
        {badge}
      </summary>
      <div className="conv-term-body">
        <div className="conv-term-copy">
          <CopyButton text={command} />
        </div>
        <pre className="conv-term-cmd conv-code--hl">
          <span className="conv-term-prompt" aria-hidden="true">
            ${' '}
          </span>
          {highlightBody(command, 'bash')}
        </pre>
        {result && (
          <>
            {/* Empty stdout + interrupted → the badge is the whole story; skip an
                empty output block. Otherwise render the stdout terminal block. */}
            {(stdout.length > 0 || (!interrupted && stderr == null)) && (
              <pre className="conv-term-out">
                <AnsiText text={stdout} />
              </pre>
            )}
            {stderr != null && (
              <pre className="conv-term-stderr">
                <AnsiText text={stderr} />
              </pre>
            )}
            {result.truncated && full == null && (
              <LoadFull
                toolUseId={call.tool_use_id ?? ''}
                which="result"
                fullLength={result.full_length ?? null}
                label="load full output"
                onLoaded={(payload) => {
                  // Capture BOTH text and stderr so the post-load re-split can
                  // restore the red stderr block (don't drop it to null).
                  if (payload.which === 'result') setFull({ text: payload.text, stderr: payload.stderr });
                }}
              />
            )}
          </>
        )}
      </div>
    </details>
  );
}
