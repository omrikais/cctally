import { useMemo } from 'react';
import type { ConversationBlock, NativePatchFile } from '../types/conversation';
import { PencilIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { UnifiedDiffView } from './UnifiedDiffView';
import { parseUnifiedDiff, type FileDiff } from './contextDiff';
import { AnsiText } from './parseAnsi';
import { NativePayloadDisclosure } from './NativePayloadDisclosure';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

function displayPath(file: NativePatchFile): string {
  const path = file.path ?? '(unknown file)';
  return file.move_path ? `${path} → ${file.move_path}` : path;
}

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
  const failed = card.success === false || card.status === 'failed' || call.result?.is_error === true;
  const changed = card.files.filter((file) => typeof file.unified_diff === 'string').length;

  return (
    <details className="conv-chip conv-chip--tool conv-native-patch" open>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <PencilIcon />
        <span className="conv-chip-name">{call.name ?? 'patch'}</span>
        <span className="conv-chip-preview">
          {card.files.length} file{card.files.length === 1 ? '' : 's'} · {changed} diff{changed === 1 ? '' : 's'}
        </span>
        {failed && <span className="conv-term-badge conv-term-badge--err">● error</span>}
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
              <span>{displayPath(file)}</span>
            </div>
            {diff ? <UnifiedDiffView files={[diff]} /> : (
              <div className="conv-native-patch-nodiff">
                No diff retained{file.raw ? ` · ${file.raw}` : ''}
              </div>
            )}
          </section>
        ))}
        {retainedDiff && (
          <p className="conv-native-patch-note">Retained diff may not be directly applicable; review before applying.</p>
        )}
        {card.stdout && <pre className="conv-term-out"><AnsiText text={card.stdout} /></pre>}
        {card.stderr && <pre className="conv-term-stderr"><AnsiText text={card.stderr} /></pre>}
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
