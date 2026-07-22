import { useState } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { TerminalIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { AnsiText } from './parseAnsi';
import { LoadFull } from './LoadFull';
import { highlightBody } from './CodeBlock';
import { useFindSplit } from './findMark';
import { useCopy } from './useCopy';
import { NativePayloadDisclosure } from './NativePayloadDisclosure';

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
// #217 S5 §4 / I-1.4 — the full-session copy text: `$ <command>` + stdout + a
// stderr block + a `… [truncated]` marker when the result is truncated and NOT
// loaded full (Codex P2-3 — copies the bounded text, never auto load-fulls).
function fullSessionText(
  command: string,
  stdout: string,
  stderr: string | null,
  truncated: boolean,
): string {
  const parts = [`$ ${command}`];
  if (stdout) parts.push(stdout.replace(/\n+$/, ''));
  if (stderr) parts.push(stderr.replace(/\n+$/, ''));
  if (truncated) parts.push('… [truncated]');
  return parts.join('\n');
}

export function BashCard({ call }: { call: Call }) {
  const split = useFindSplit();
  const { copied: copiedFull, copy: copyFull } = useCopy();
  // The full output loaded on demand when result.truncated (#178); supersedes
  // the bounded result.text for rendering. The load-full result payload carries
  // a discrete `stderr` field, so we capture it alongside `text` and re-split
  // exactly like the un-truncated path — the red stderr block must survive a
  // load-full (it's precisely when the user asked to see MORE).
  const [full, setFull] = useState<{ text: string; stderr: string | null | undefined } | null>(null);

  const native = call.native_card?.type === 'terminal' ? call.native_card : null;
  const commands = native?.commands ?? [{
    command: commandOf(call),
    workdir: typeof (call.input as { workdir?: unknown } | null | undefined)?.workdir === 'string'
      ? (call.input as { workdir: string }).workdir
      : null,
    metadata: {},
  }];
  const command = commands.map((entry) => entry.command).join('\n');
  const result = call.result;
  const nativeOutput = native?.output;
  const isError = nativeOutput?.is_error === true || result?.is_error === true;
  const interrupted = call.interrupted === true || native?.status === 'interrupted';

  // After load-full, re-split the LOADED text against the LOADED stderr (same
  // splitStreams contract as the bounded path); before, split the capped
  // result.text against the block-level call.stderr.
  const rawText = full?.text ?? result?.text ?? '';
  const nativeStdout = nativeOutput?.parts
    .filter((part) => part.type === 'text' && part.stream !== 'stderr')
    .map((part) => part.text).join('');
  const nativeStderr = nativeOutput?.parts
    .filter((part) => part.type === 'text' && part.stream === 'stderr')
    .map((part) => part.text).join('');
  const rawParts = nativeOutput?.parts.filter((part) => part.type === 'raw') ?? [];
  const legacyStreams = full == null
    ? splitStreams(rawText, call.stderr)
    : splitStreams(rawText, full.stderr);
  const stdout = full == null && nativeOutput ? nativeStdout ?? '' : legacyStreams.stdout;
  const stderr = full == null && nativeOutput
    ? (nativeStderr && nativeStderr.length > 0 ? nativeStderr : null)
    : legacyStreams.stderr;

  const badge = isError ? (
    <span className="conv-term-badge conv-term-badge--err">● error</span>
  ) : interrupted ? (
    <span className="conv-term-badge conv-term-badge--int">■ interrupted</span>
  ) : null;

  // #217 S3 E9 — collapse heuristic. A long terminal output buries the next turn
  // when always-open, so a card whose RENDERED output (stdout + stderr) exceeds a
  // fixed line threshold opens COLLAPSED with a "show N lines" hint; short output
  // stays open. Counted on the rendered streams (not the raw command/payload).
  // No config key (Q4). The [/] collapse-all and per-card click still override
  // (they set `.open` imperatively on the DOM element, above whatever React
  // renders). A request-only card (no result → nothing to collapse) stays open.
  const COLLAPSE_LINE_THRESHOLD = 20;
  const outputLineCount =
    result == null
      ? 0
      : (stdout.length > 0 ? stdout.split('\n').length : 0) +
        (stderr != null && stderr.length > 0 ? stderr.split('\n').length : 0) +
        rawParts.reduce((total, part) => total + Math.max(1, part.text.split('\n').length), 0);
  const collapseLong = outputLineCount > COLLAPSE_LINE_THRESHOLD;

  return (
    <details className="conv-chip conv-chip--tool conv-term" open={!collapseLong}>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <TerminalIcon />
        <span className="conv-chip-name">{native ? call.name ?? 'exec' : 'Bash'}</span>
        <span className="conv-chip-preview">{descriptionOf(call) ?? call.preview}</span>
        {badge}
        {/* #217 S3 E9 — collapsed-by-default hint; the disclosure arrow already
            affords expansion, this names the hidden line count. Hidden once the
            user (or collapse-all) opens the card via the [open] sibling rule. */}
        {collapseLong && (
          <span className="conv-term-collapsed-hint">show {outputLineCount} lines</span>
        )}
      </summary>
      <div className="conv-term-body">
        <div className="conv-term-copy">
          <CopyButton text={command} />
          {/* #217 S5 §4 — copy the full session output ($ cmd + stdout + stderr
              + a [truncated] marker when the result is clipped and not loaded). */}
          <button
            type="button"
            className="conv-term-copyfull"
            aria-label={copiedFull ? 'Copied full session' : 'Copy full session'}
            title="Copy full session ($ cmd + output)"
            onClick={(e) => {
              e.stopPropagation();
              copyFull(
                fullSessionText(
                  command,
                  stdout,
                  stderr,
                  result?.truncated === true && full == null,
                ),
              );
            }}
          >
            {copiedFull ? '✓ full' : 'copy full'}
          </button>
        </div>
        {commands.map((entry, index) => (
          <div className="conv-term-command" key={`${index}-${entry.command}`}>
            {entry.workdir && <div className="conv-term-workdir">{entry.workdir}</div>}
            <pre className="conv-term-cmd conv-code--hl">
              <span className="conv-term-prompt" aria-hidden="true">
                ${' '}
              </span>
              {highlightBody(entry.command, 'bash', split)}
            </pre>
          </div>
        ))}
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
            {full == null && rawParts.map((part, index) => (
              <pre className="conv-term-raw" key={`${index}-${part.text.slice(0, 24)}`}>
                <span className="conv-term-raw-label">unparsed output</span>{'\n'}
                {part.text}
              </pre>
            ))}
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
            {native && call.tool_use_id && (
              <div className="conv-native-raw-actions">
                <NativePayloadDisclosure blockKey={call.tool_use_id} which="input" label="request" />
                <NativePayloadDisclosure blockKey={call.tool_use_id} which="result" label="output" />
              </div>
            )}
          </>
        )}
        {native && result == null && call.tool_use_id && (
          <div className="conv-native-raw-actions">
            <NativePayloadDisclosure blockKey={call.tool_use_id} which="input" label="request" />
          </div>
        )}
      </div>
    </details>
  );
}
