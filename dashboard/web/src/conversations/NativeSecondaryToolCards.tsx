import type { ConversationBlock, NativeToolCard } from '../types/conversation';
import { dispatch } from '../store/store';
import { ChecklistCard } from './ChecklistCard';
import { CopyButton } from './CopyButton';
import { PlugIcon, SubagentIcon } from './ConvIcons';
import { NativePayloadDisclosure } from './NativePayloadDisclosure';

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
  const card = call.native_card?.type === 'mcp' ? call.native_card : null;
  if (!card) return null;
  const failed = card.call_status === 'failed' || card.completion.status === 'error';
  const request = json(card.completion.arguments);
  const result = json(card.completion.result);
  return (
    <details className={`conv-chip conv-chip--tool conv-native-mcp${failed ? ' conv-native-card--error' : ''}`} open>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <PlugIcon />
        <span className="conv-chip-name" title={card.name}>{card.completion.tool}</span>
        <span className="conv-chip-server">{card.completion.server}</span>
        <span className="conv-chip-preview">MCP · {mcpDuration(card)}</span>
        <span className="conv-chip-status">· {failed ? 'error' : card.completion.status}</span>
      </summary>
      <div className="conv-chip-body conv-chip-body--io">
        <div className="conv-tool-io">
          <div className="conv-tool-io-label">request</div><CopyButton text={request} /><pre className="conv-code">{request}</pre>
        </div>
        <div className="conv-tool-io">
          <div className="conv-tool-io-label">result · {card.completion.status}</div><CopyButton text={result} /><pre className="conv-code conv-code--result">{result}</pre>
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
  const card = call.native_card?.type === 'agent' ? call.native_card : null;
  if (!card) return null;
  const request = json(card.arguments);
  const result = card.result ? json(card.result.value) : null;
  const child = card.child_conversation;
  return (
    <details className="conv-chip conv-chip--tool conv-native-agent" open>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <SubagentIcon />
        <span className="conv-chip-name">{AGENT_LABELS[card.operation]}</span>
        <span className="conv-chip-preview">{agentPreview(card)}</span>
        <span className="conv-chip-status">· {card.result?.status ?? card.call_status}</span>
      </summary>
      <div className="conv-chip-body conv-chip-body--io">
        {child && (
          <button
            type="button"
            className="conv-thread-link conv-native-agent-child"
            onClick={() => dispatch({ type: 'SELECT_CONVERSATION', conversationRef: { source: 'codex', key: child.conversation_key } })}
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
