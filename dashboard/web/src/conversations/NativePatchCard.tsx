import { useMemo } from 'react';
import type { ConversationBlock, NativePatchFile } from '../types/conversation';
import { FAILED_STATUSES } from '../lib/conversationAdapters';
import { PencilIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { UnifiedDiffView } from './UnifiedDiffView';
import { parseUnifiedDiff, type FileDiff } from './contextDiff';
import { AnsiText } from './parseAnsi';
import { NativePayloadDisclosure } from './NativePayloadDisclosure';
import { OutcomeBadge, OutcomeEvidence } from './OutcomeBadge';
import { splitToExactNodes, useExactSurfaceTargets } from './findMark';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

function parsedFile(file: NativePatchFile): FileDiff | null {
  if (typeof file.unified_diff !== 'string') return null;
  // Task A retains per-file unified diffs, which need not carry a `diff --git`
  // marker. Add a parsing-only sentinel, then restore the provider paths. The
  // rendered rows remain byte-derived from the retained hunk itself.
  const parsed = parseUnifiedDiff(`diff --git a/__native__ b/__native__\n${file.unified_diff}`)[0];
  if (!parsed) return null;
  return {
    ...parsed,
    oldPath: file.path ?? '(unknown file)',
    newPath: file.status === 'deleted' ? '/dev/null' : file.move_path ?? file.path ?? '(unknown file)',
  };
}

export function NativePatchCard({ call }: { call: Call }) {
  const exact = useExactSurfaceTargets(call.block_key, 'completion');
  const card = call.native_card?.type === 'patch' ? call.native_card : null;
  const parsed = useMemo(
    () => card?.files.map((file) => ({ file, diff: parsedFile(file) })) ?? [],
    [card],
  );
  if (!card) return null;
  const retainedDiff = card.files
    .map((file) => file.unified_diff)
    .filter((diff): diff is string => typeof diff === 'string')
    .join('\n');
  // #463 S4 remediation round 4 — the shared `FAILED_STATUSES`, not a second
  // private copy of the vocabulary. This card decides its own badge from the
  // card rather than from the adapted `result`, so a bare `'failed'` literal
  // here contradicted the server's `classify_tool_failure` on `status: "error"`
  // independently of what the adapter did.
  const failed = card.success === false || FAILED_STATUSES.has(card.status) || call.result?.is_error === true;
  const changed = card.files.filter((file) => typeof file.unified_diff === 'string').length;

  return (
    <details className="conv-chip conv-chip--tool conv-native-patch" open
             {...(call.block_key ? { 'data-disclosure-key': call.block_key } : {})}>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <PencilIcon />
        <span className="conv-chip-name">{call.name ?? 'patch'}</span>
        <span className="conv-chip-preview">
          {card.files.length} file{card.files.length === 1 ? '' : 's'} · {changed} diff{changed === 1 ? '' : 's'}
        </span>
        {failed ? (
          <span className="conv-term-badge conv-term-badge--err">● error</span>
        ) : call.outcome ? (
          <OutcomeBadge outcome={call.outcome} truncated={call.result?.truncated === true} />
        ) : null}
      </summary>
      <div className="conv-native-patch-body">
        {retainedDiff && (
          <div className="conv-native-patch-copy">
            <CopyButton text={retainedDiff} />
          </div>
        )}
        {parsed.map(({ file, diff }, index) => (
          <section className="conv-native-patch-file" key={`${index}-${file.path ?? file.raw ?? 'file'}`}>
            <div className="conv-native-patch-filehead">
              <span className="conv-native-patch-status">{file.status ?? 'unknown'}</span>
              <span>
                {exact.get(`files.${index}.path`)?.length
                  ? splitToExactNodes(file.path ?? '(unknown file)', exact.get(`files.${index}.path`)!)
                  : file.path ?? '(unknown file)'}
                {file.move_path && (
                  <>{' → '}{exact.get(`files.${index}.move_path`)?.length
                    ? splitToExactNodes(file.move_path, exact.get(`files.${index}.move_path`)!)
                    : file.move_path}</>
                )}
              </span>
              {/* #463 S3 §3.1 — THIS FILE's own text was cut, which is
                  independent of the card-level `truncated` below. An entry from
                  a server that predates S3 carries no flag and reads as whole. */}
              {file.truncated === true && (
                <span className="conv-native-patch-fileclipped">clipped</span>
              )}
            </div>
            {diff ? (
              <>
                <UnifiedDiffView
                  files={[diff]}
                  exactTargetsForRow={(_fileIndex, hunkIndex, rowIndex) =>
                    exact.get(`files.${index}.diff.${hunkIndex}.${rowIndex}`) ?? []}
                />
                {/* A synthesized diff is a rendering of the retained file
                    content, so the card must not present it as bytes the
                    provider transmitted. */}
                {file.diff_source === 'derived' && (
                  <div className="conv-native-patch-derived">rendered from retained content</div>
                )}
              </>
            ) : file.truncated === true ? (
              // Wire contract §4: a diff clipped at line boundaries can leave
              // nothing renderable — a minified file is one physical line for
              // its whole body. That is "a diff exists and none of it fit",
              // which is a different fact from "no diff was retained".
              <div className="conv-native-patch-nodiff">
                A diff exists for this file and none of it fit the card's size budget · raw payload available
              </div>
            ) : (
              // "No diff retained" reads as a claim about what the provider
              // chose to keep. What the card knows is narrower: this change
              // carries no line-level diff, whoever decided that.
              <div className="conv-native-patch-nodiff">
                This change carries no line-level diff{file.raw ? ` · ${file.raw}` : ''}
              </div>
            )}
          </section>
        ))}
        {retainedDiff && (
          <p className="conv-native-patch-note">Retained diff may not be directly applicable; review before applying.</p>
        )}
        {/* Rendered whenever the call carries an outcome, including the failed
            branch above where the summary shows the error badge instead — the
            exit code of a failed patch is the reader's most useful fact. */}
        {call.outcome && <OutcomeEvidence outcome={call.outcome} />}
        {card.stdout && <pre className="conv-term-out">
          <AnsiText text={card.stdout} exactTargets={exact.get('stdout') ?? []} />
        </pre>}
        {card.stderr && <pre className="conv-term-stderr">
          <AnsiText text={card.stderr} exactTargets={exact.get('stderr') ?? []} />
        </pre>}
        {card.truncated && <div className="conv-native-patch-truncated">Capped view · raw payload available</div>}
        {call.payload_capable && call.tool_use_id && (
          <div className="conv-native-raw-actions">
            {call.payload_kind === 'event' ? (
              <NativePayloadDisclosure blockKey={call.tool_use_id} which="event" label="event" />
            ) : (
              <>
                <NativePayloadDisclosure blockKey={call.tool_use_id} which="input" label="request" />
                {call.result && <NativePayloadDisclosure blockKey={call.tool_use_id} which="result" label="output" />}
              </>
            )}
            {card.event_payload_key && (
              <NativePayloadDisclosure blockKey={card.event_payload_key} which="event" label="event" />
            )}
          </div>
        )}
      </div>
    </details>
  );
}
