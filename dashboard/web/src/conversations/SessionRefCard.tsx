import type { ConversationBlock } from '../types/conversation';
import { TerminalIcon } from './ConvIcons';
import { AnsiText } from './parseAnsi';
import { OutcomeBadge, OutcomeEvidence } from './OutcomeBadge';
import { useSessionIndex } from './TranscriptContext';
import {
  openerState, sessionCharsPreview, sessionOperationLabel,
  sessionOperationLabelNarrow, sessionReferenceLabel, sessionReferenceLabelNarrow,
} from './sessionIndex';
import { NativePayloadDisclosure } from './NativePayloadDisclosure';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// #463 S3 §3.2 — the reference a `session_ref` card carries. At `shell` scope
// it is the conversation-local session ORDINAL the server assigned over the
// whole conversation; at `cell` scope it is the sandbox cell id as given. The
// two namespaces have zero overlapping values, so a cell is never described as
// a shell session and is never grouped by.
//
// A null ref renders NOTHING. The server registers a session only from a
// standalone `write_stdin` row, so a session named only inside a program body
// has no ordinal — roughly a fifth of them. The client is served no provider
// identifier to fall back on, and inventing an ordinal here would make two
// different sessions read as one.
export function SessionReference({ scope, sessionRef }: {
  scope: 'shell' | 'cell';
  sessionRef: string | null;
}) {
  if (sessionRef == null) return null;
  // A rigid summary child, so it ships both wordings and lets the shipped
  // .conv-status-wide/.conv-status-narrow media pair pick one at 640px.
  return (
    <span className={`conv-session-ref conv-session-ref--${scope}`}>
      <span className="conv-status-wide">{sessionReferenceLabel(scope, sessionRef)}</span>
      <span className="conv-status-narrow">{sessionReferenceLabelNarrow(scope, sessionRef)}</span>
    </span>
  );
}

// What the call did. Measured in-browser at 390px, this was the ONE new rigid
// summary child shipping a single wording, and it starved the flexible preview
// — the chip's primary identifier — to about 21px on a `write_stdin` row. It
// now ships a pair like every rigid neighbour.
export function SessionOperation({ operation }: { operation: 'write' | 'poll' }) {
  return (
    <span className="conv-session-op">
      <span className="conv-status-wide">{sessionOperationLabel(operation)}</span>
      <span className="conv-status-narrow">{sessionOperationLabelNarrow(operation)}</span>
    </span>
  );
}

// §5.3 — the three statements about an opener, kept distinct in wording as well
// as in the state that selects them.
const OPENER_NOTES: Record<'not_retained' | 'index_truncated', string> = {
  not_retained: 'opener not in this conversation\u2019s retained data',
  index_truncated: 'The session index was truncated, so this session\u2019s opener may not have been loaded.',
};

// A `write_stdin` or `wait` call (#463 S3 §5.4). The provider tool name is
// rendered verbatim — `exec` is never relabelled Bash and these are never
// relabelled either. Collapsed by default: a shell session produces long runs
// of these, and 218 consecutive open cards would bury the turn.
export function SessionRefCard({ call }: { call: Call }) {
  const index = useSessionIndex();
  const card = call.native_card?.type === 'session_ref' ? call.native_card : null;
  if (!card) return null;
  const chars = card.chars;
  const opener = openerState(index, card.scope, card.ref, call.tool_use_id);
  return (
    <details className="conv-chip conv-chip--tool conv-session-ref-card">
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <TerminalIcon />
        <span className="conv-chip-name">{call.name ?? 'session'}</span>
        <SessionOperation operation={card.operation} />
        <SessionReference scope={card.scope} sessionRef={card.ref} />
        {opener === 'opener' && (
          <span className="conv-session-opener">
            <span className="conv-status-wide">started this session</span>
            <span className="conv-status-narrow">start</span>
          </span>
        )}
        <span className="conv-chip-preview">
          {chars != null && <span className="conv-session-chars">{sessionCharsPreview(chars)}</span>}
        </span>
        {call.outcome && (
          <OutcomeBadge outcome={call.outcome} isError={call.result?.is_error === true}
                        truncated={call.result?.truncated === true} />
        )}
      </summary>
      <div className="conv-session-body">
        {/* The expanded card has room for the full statement; the collapsed row
            carries only the badge. */}
        {(opener === 'not_retained' || opener === 'index_truncated') && (
          <p className="conv-session-note">{OPENER_NOTES[opener]}</p>
        )}
        {chars != null && (
          <div className="conv-session-written">
            <div className="conv-tool-io-label">characters written</div>
            <pre className="conv-term-out"><AnsiText text={chars} /></pre>
          </div>
        )}
        {call.outcome && <OutcomeEvidence outcome={call.outcome} />}
        {call.result && (
          <div className="conv-session-result">
            <div className="conv-tool-io-label">result</div>
            <pre className="conv-term-out"><AnsiText text={call.result.text} /></pre>
          </div>
        )}
        {card.truncated && (
          <p className="conv-session-truncated">Capped view · raw payload available</p>
        )}
        {call.payload_capable && call.tool_use_id && (
          <div className="conv-native-raw-actions">
            <NativePayloadDisclosure blockKey={call.tool_use_id} which="input" label="request" />
            {call.result && <NativePayloadDisclosure blockKey={call.tool_use_id} which="result" label="output" />}
          </div>
        )}
      </div>
    </details>
  );
}
