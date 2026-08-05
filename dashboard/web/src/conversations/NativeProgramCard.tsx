import type { ConversationBlock, NativeProgramInvocation } from '../types/conversation';
import { TerminalIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { AnsiText } from './parseAnsi';
import { highlightBody } from './CodeBlock';
import { useFindSplit } from './findMark';
import { SessionOperation, SessionReference } from './SessionRefCard';
import { NativePayloadDisclosure } from './NativePayloadDisclosure';
import { OutcomeBadge, OutcomeEvidence } from './OutcomeBadge';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// One line per invocation the bounded lexical scanner recognized. The three
// kinds are presented apart because they claim different things: a `command`
// entry carries the command the program ran, a `session` entry carries a
// reference into the conversation's shell sessions or sandbox cells, and an
// `other` entry names a tool whose arguments the closed literal parser
// declined — so it must claim nothing at all about what that tool was given.
function InvocationRow({ invocation, split }: {
  invocation: NativeProgramInvocation;
  split: ReturnType<typeof useFindSplit>;
}) {
  if (invocation.kind === 'command') {
    return (
      <li className="conv-program-invocation conv-program-invocation--command">
        {invocation.workdir && <span className="conv-term-workdir">{invocation.workdir}</span>}
        <pre className="conv-term-cmd conv-code--hl">
          <span className="conv-term-prompt" aria-hidden="true">${' '}</span>
          {highlightBody(invocation.command, 'bash', split)}
        </pre>
      </li>
    );
  }
  if (invocation.kind === 'session') {
    return (
      <li className="conv-program-invocation conv-program-invocation--session">
        <SessionOperation operation={invocation.operation} />
        <SessionReference scope={invocation.scope} sessionRef={invocation.ref} />
        {invocation.chars != null && <span className="conv-session-chars">{invocation.chars}</span>}
      </li>
    );
  }
  return (
    <li className="conv-program-invocation conv-program-invocation--other">
      <span className="conv-program-tool">{invocation.name}</span>
      <span className="conv-program-other-note">arguments not read</span>
    </li>
  );
}

// A `custom_tool_call` `exec` whose body is a JavaScript program, or a `js`
// call (#463 S3 §3.3). The provider tool name is rendered verbatim: `exec` is
// never relabelled Bash.
export function NativeProgramCard({ call }: { call: Call }) {
  const split = useFindSplit();
  const card = call.native_card?.type === 'program' ? call.native_card : null;
  if (!card) return null;
  const commandText = card.invocations
    .filter((invocation): invocation is Extract<NativeProgramInvocation, { kind: 'command' }> => invocation.kind === 'command')
    .map((invocation) => invocation.command)
    .join('\n');
  return (
    <details className="conv-chip conv-chip--tool conv-program">
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <TerminalIcon />
        <span className="conv-chip-name">{call.name ?? 'program'}</span>
        {/* The adapter's `programPreview` is the single source of this string.
            A second copy here drifted from it. */}
        <span className="conv-chip-preview">{call.preview}</span>
        {call.outcome && (
          <OutcomeBadge outcome={call.outcome} isError={call.result?.is_error === true}
                        truncated={call.result?.truncated === true} />
        )}
      </summary>
      <div className="conv-program-body">
        {card.title && <div className="conv-program-title">{card.title}</div>}
        {commandText && (
          <div className="conv-program-copy"><CopyButton text={commandText} /></div>
        )}
        <ol className="conv-program-invocations">
          {card.invocations.map((invocation, index) => (
            <InvocationRow key={`${index}-${invocation.kind}`} invocation={invocation} split={split} />
          ))}
        </ol>
        {/* §3.3 — the scanner models `tools.<name>(` and nothing else, so a
            program it could not fully read certainly did more than this list
            shows. Saying so is the whole reason `complete` exists. */}
        {!card.complete && (
          <p className="conv-program-incomplete">
            This program also runs statements the reader could not read, so it did more than the list above shows.
          </p>
        )}
        {card.truncated && (
          <p className="conv-program-truncated">Invocation list capped · raw payload available</p>
        )}
        {call.outcome && <OutcomeEvidence outcome={call.outcome} />}
        {call.result && (
          <div className="conv-program-result">
            <div className="conv-tool-io-label">result</div>
            <pre className="conv-term-out"><AnsiText text={call.result.text} /></pre>
          </div>
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
