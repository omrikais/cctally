import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { ConversationBlock, NativeToolCard } from '../types/conversation';
import { _resetForTests, getState } from '../store/store';
import { NativeAgentCard, NativeMcpCard, NativePlanCard } from './NativeSecondaryToolCards';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

function call(native_card: NativeToolCard): Call {
  return {
    kind: 'tool_call', name: native_card.type, input_summary: '{}', input: {}, preview: native_card.type,
    tool_use_id: 'cbk.call', payload_capable: true, payload_kind: 'call', native_card,
    result: { text: 'result', truncated: false, is_error: false },
  };
}

beforeEach(() => _resetForTests());

describe('Codex Session C native cards', () => {
  it('shows plan progress, explanation, and completion state', () => {
    const { container } = render(<NativePlanCard call={call({
      schema_version: 1, type: 'plan', source: 'update_plan', call_status: 'requested',
      explanation: 'Synthetic plan explanation',
      items: [{ step: 'Done', status: 'completed' }, { step: 'Active', status: 'in_progress' }],
      result: { status: 'returned', value: 'Plan updated', truncated: false },
    })} />);
    expect(container.textContent).toContain('1 / 2');
    expect(container.textContent).toContain('Synthetic plan explanation');
    expect(container.textContent).toContain('Plan updated');
  });

  it('keeps an interrupted plan and pending agent native without inventing results', () => {
    const plan = call({
      schema_version: 1, type: 'plan', source: 'update_plan', call_status: 'interrupted',
      explanation: null, items: [{ step: 'Still pending', status: 'pending' }],
    });
    plan.result = null;
    const { container, rerender } = render(<NativePlanCard call={plan} />);
    expect(container.textContent).toContain('interrupted');
    expect(container.textContent).toContain('Still pending');
    expect(container.textContent).not.toContain('result ·');

    const agent = call({
      schema_version: 1, type: 'agent', operation: 'wait_agent', call_status: 'requested',
      arguments: { timeout_ms: 30_000 },
    });
    agent.result = null;
    rerender(<NativeAgentCard call={agent} />);
    expect(container.textContent).toContain('requested');
    expect(container.textContent).not.toContain('result ·');
  });

  it('keeps MCP server/tool identity, duration, error, and raw payload controls', () => {
    const { container } = render(<NativeMcpCard call={call({
      schema_version: 1, type: 'mcp', source: 'function_call', name: 'fixture_get_issue', call_status: 'failed',
      completion: {
        status: 'error', server: 'fixture', tool: 'get_issue', arguments: { number: 999 },
        result: { Err: 'synthetic MCP failure' }, duration: { secs: 0, nanos: 500_000_000 }, event_block_key: 'cbk.event',
      },
    })} />);
    expect(container.textContent).toContain('get_issue');
    expect(container.textContent).toContain('fixture');
    expect(container.textContent).toContain('500ms');
    expect(container.textContent).toContain('synthetic MCP failure');
    expect(screen.getByRole('button', { name: /raw request/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /raw event/i })).toBeTruthy();
  });

  it('links only a proven child conversation using the exact opaque key', () => {
    const linked = call({
      schema_version: 1, type: 'agent', operation: 'spawn_agent', call_status: 'requested', arguments: { task_name: 'child' },
      result: { status: 'returned', value: { task_name: '/root/child' }, truncated: false },
      child_conversation: { conversation_key: 'v1.exact-child', role: 'cctally_reviewer', nickname: 'Synthetic Child' },
    });
    const { rerender } = render(<NativeAgentCard call={linked} />);
    fireEvent.click(screen.getByRole('button', { name: /open child.*synthetic child/i }));
    expect(getState().selectedConversationRef).toEqual({ source: 'codex', key: 'v1.exact-child' });

    rerender(<NativeAgentCard call={call({
      schema_version: 1, type: 'agent', operation: 'spawn_agent', call_status: 'requested', arguments: { task_name: 'ambiguous' },
      result: { status: 'returned', value: { task_name: '/root/ambiguous' }, truncated: false },
    })} />);
    expect(screen.queryByRole('button', { name: /open child/i })).toBeNull();
  });
});
