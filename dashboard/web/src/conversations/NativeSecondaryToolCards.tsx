import type { ConversationBlock, NativeToolCard } from '../types/conversation';
import { FAILED_STATUSES } from '../lib/conversationAdapters';
import { dispatch } from '../store/store';
import { ChecklistCard } from './ChecklistCard';
import { CopyButton } from './CopyButton';
import { PlugIcon, SubagentIcon } from './ConvIcons';
import { NativePayloadDisclosure } from './NativePayloadDisclosure';
import { OutcomeBadge, OutcomeEvidence, outcomeFromStatus } from './OutcomeBadge';
import { splitToExactNodes, useExactSurfaceTargets } from './findMark';
import { useConversationRef } from './TranscriptContext';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

function json(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function payloadActions(call: Call, eventBlockKey?: string) {
  if (!call.payload_capable || !call.tool_use_id) return null;
  return (
    <div className="conv-native-raw-actions">
      <NativePayloadDisclosure blockKey={call.tool_use_id} which="input" label="request" />
      {call.result && <NativePayloadDisclosure blockKey={call.tool_use_id} which="result" label="output" />}
      {eventBlockKey && <NativePayloadDisclosure blockKey={eventBlockKey} which="event" label="event" />}
    </div>
  );
}

export function NativePlanCard({ call }: { call: Call }) {
  const card = call.native_card?.type === 'plan' ? call.native_card : null;
  if (!card) return null;
  const value = card.result ? json(card.result.value) : undefined;
  return (
    <div className="conv-native-plan">
      <ChecklistCard
        label="Plan"
        todos={card.items.map((item) => ({ content: item.step, status: item.status }))}
        description={card.explanation}
        statusText={card.result?.status ?? card.call_status}
        resultText={value}
      />
      {payloadActions(call)}
    </div>
  );
}

function mcpDuration(card: Extract<NativeToolCard, { type: 'mcp' }>): string {
  const ms = card.completion.duration.secs * 1000 + card.completion.duration.nanos / 1_000_000;
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`;
}

export function NativeMcpCard({ call }: { call: Call }) {
  const exact = useExactSurfaceTargets(call.block_key, 'completion');
  const card = call.native_card?.type === 'mcp' ? call.native_card : null;
  if (!card) return null;
  // #463 S4 remediation round 6 — the verdict reads ONE axis,
  // `completion.status`, through the shared `FAILED_STATUSES`.
  //
  // Round 5 corrected a real inversion: this read the two literals
  // `call_status === 'failed'` and `completion.status === 'error'`, which are
  // the same vocabulary EXCHANGED, so each axis recognised only the word the
  // other one used. It converted both axes to the shared set, and reading
  // `call_status` at all is the mirror image of the defect it closed. The
  // server's `classify_tool_failure` reads `completion.status` alone for the
  // mcp family, so a failing `call_status` would render a card failure that the
  // Errors badge never counts and the Errors filter never surfaces — the same
  // cross-surface disagreement pointing the other way. `call_status` is inert
  // in the retained data (`"requested"` on every scanned `function_call`
  // payload; defaulted in `_lib_codex_conversation_query.py` and overridden
  // only from a payload key that does not occur), so dropping it changes no
  // rendered card. Do not reintroduce it: the client must read the axis the
  // server decides on.
  const failed = FAILED_STATUSES.has(card.completion.status);
  const outcome = call.outcome ?? outcomeFromStatus(card.completion.status, failed);
  const request = json(card.completion.arguments);
  const result = json(card.completion.result);
  return (
    <details className={`conv-chip conv-chip--tool conv-native-mcp${failed ? ' conv-native-card--error' : ''}`} open
             {...(call.block_key ? { 'data-disclosure-key': call.block_key } : {})}>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <PlugIcon />
        <span className="conv-chip-name" title={card.name}>{card.completion.tool}</span>
        <span className="conv-chip-server">{card.completion.server}</span>
        <span className="conv-chip-preview">MCP · {mcpDuration(card)}</span>
        <OutcomeBadge outcome={outcome} isError={failed} />
      </summary>
      <div className="conv-chip-body conv-chip-body--io">
        <OutcomeEvidence outcome={outcome} />
        <div className="conv-tool-io">
          <div className="conv-tool-io-label">request</div><CopyButton text={request} /><pre className="conv-code">{
            exact.get('arguments')?.length
              ? splitToExactNodes(request, exact.get('arguments')!)
              : request
          }</pre>
        </div>
        <div className="conv-tool-io">
          <div className="conv-tool-io-label">result · {card.completion.status}</div><CopyButton text={result} /><pre className="conv-code conv-code--result">{
            exact.get('result')?.length
              ? splitToExactNodes(result, exact.get('result')!)
              : result
          }</pre>
        </div>
        {payloadActions(call, card.completion.event_block_key)}
      </div>
    </details>
  );
}

const AGENT_LABELS: Record<Extract<NativeToolCard, { type: 'agent' }>['operation'], string> = {
  spawn_agent: 'Spawn agent', wait_agent: 'Wait for agents', send_message: 'Send message',
  list_agents: 'List agents', followup_task: 'Follow up task', interrupt_agent: 'Interrupt agent',
};

function agentPreview(card: Extract<NativeToolCard, { type: 'agent' }>): string {
  const args = card.arguments;
  if (typeof args.task_name === 'string') return args.task_name;
  if (typeof args.target === 'string') return args.target;
  if (typeof args.timeout_ms === 'number') return `${args.timeout_ms}ms`;
  if (card.operation === 'list_agents') return 'current team';
  return card.operation;
}

export function NativeAgentCard({ call }: { call: Call }) {
  // Called BEFORE the `!card` early return: a hook below a conditional return
  // violates rules-of-hooks and blanks the reader on the first render that takes
  // the other branch.
  const currentRef = useConversationRef();
  const card = call.native_card?.type === 'agent' ? call.native_card : null;
  if (!card) return null;
  const request = json(card.arguments);
  const result = card.result ? json(card.result.value) : null;
  const child = card.child_conversation;
  const status = card.result?.status ?? card.call_status;
  const outcome = call.outcome ?? outcomeFromStatus(status, call.result?.is_error === true);
  return (
    <details className="conv-chip conv-chip--tool conv-native-agent" open>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <SubagentIcon />
        <span className="conv-chip-name">{AGENT_LABELS[card.operation]}</span>
        <span className="conv-chip-preview">{agentPreview(card)}</span>
        <OutcomeBadge outcome={outcome} isError={call.result?.is_error === true} />
      </summary>
      <div className="conv-chip-body conv-chip-body--io">
        <OutcomeEvidence outcome={outcome} />
        {child && (
          <button
            type="button"
            className="conv-thread-link conv-native-agent-child"
            // #463 S5 (F24d, spec §4.6) — carry the current conversation's
            // account into the child ref. `account_key` is part of conversation
            // identity, so an accountless child ref opens an identity no rail
            // row compares as current while the chip still names an account. The
            // reader publishes its ref on TranscriptContext, so this stays a
            // ref-construction change with no new prop threading. Codex-only
            // card, and an account key is scoped to one provider, so the source
            // guard keeps a Claude account from leaking onto a Codex ref.
            onClick={() => dispatch({
              type: 'SELECT_CONVERSATION',
              conversationRef: currentRef?.account_key && currentRef.source === 'codex'
                ? { source: 'codex', key: child.conversation_key, account_key: currentRef.account_key }
                : { source: 'codex', key: child.conversation_key },
            })}
          >
            Open child → {child.nickname || child.role || 'conversation'}
          </button>
        )}
        <div className="conv-tool-io">
          <div className="conv-tool-io-label">request</div><CopyButton text={request} /><pre className="conv-code">{request}</pre>
        </div>
        {card.result && result != null && (
          <div className="conv-tool-io">
            <div className="conv-tool-io-label">result · {card.result.status}</div><CopyButton text={result} /><pre className="conv-code conv-code--result">{result}</pre>
          </div>
        )}
        {payloadActions(call)}
      </div>
    </details>
  );
}
