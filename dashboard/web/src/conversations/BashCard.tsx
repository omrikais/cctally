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
  // the bounded result.text for rendering.
  const [fullText, setFullText] = useState<string | null>(null);

  const command = commandOf(call);
  const result = call.result;
  const isError = result?.is_error === true;
  const interrupted = call.interrupted === true;

  const rawText = fullText ?? result?.text ?? '';
  // When we've loaded the full payload, the bounded stderr suffix may no longer
  // line up; keep the stderr split keyed on call.stderr only while showing the
  // capped text. After load-full the full text is rendered whole.
  const { stdout, stderr } = fullText == null ? splitStreams(rawText, call.stderr) : { stdout: rawText, stderr: null };

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
        <span className="conv-chip-preview">{call.preview}</span>
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
            {result.truncated && fullText == null && (
              <LoadFull
                toolUseId={call.tool_use_id ?? ''}
                which="result"
                fullLength={result.full_length ?? null}
                label="load full output"
                onLoaded={(payload) => {
                  if (payload.which === 'result') setFullText(payload.text);
                }}
              />
            )}
          </>
        )}
      </div>
    </details>
  );
}
