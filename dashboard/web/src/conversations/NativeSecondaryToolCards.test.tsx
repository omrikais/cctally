import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { ConversationBlock, NativeToolCard } from '../types/conversation';
import { _resetForTests, getState } from '../store/store';
import { NativeAgentCard, NativeMcpCard, NativePlanCard } from './NativeSecondaryToolCards';
import { ExactFindContext } from './HighlightContext';
import { TranscriptContext } from './TranscriptContext';

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
    expect(container.querySelector('.conv-outcome')?.textContent).toContain('running');
    expect(container.textContent).not.toContain('requested');
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

  // #463 S4 remediation round 5 — the MCP card decided failure with
  // `card.call_status === 'failed' || card.completion.status === 'error'`: two
  // bare literals whose vocabularies were EXCHANGED relative to each other and
  // relative to `FAILED_STATUSES`. The server's `classify_tool_failure` fails an
  // MCP completion on `status in {"failed","error"}` and publishes a
  // `tool_error` landmark, which `outputError` turns into `result.is_error` and
  // `itemHasError` counts, so a completion carrying `failed` would have been
  // counted by the Errors badge and shown by the Errors filter while this card
  // rendered no failure treatment at all.
  it('reads FAILED_STATUSES on the completion axis rather than one bare literal', () => {
    const { container } = render(<NativeMcpCard call={call({
      schema_version: 1, type: 'mcp', source: 'function_call', name: 'fixture_tool',
      call_status: 'requested',
      completion: {
        status: 'failed', server: 'fixture', tool: 'get_issue', arguments: {},
        result: { Err: 'synthetic failure' }, duration: { secs: 0, nanos: 1_000_000 },
      },
    })} />);
    expect(container.querySelector('.conv-native-card--error')).toBeTruthy();
    expect(container.querySelector('.conv-outcome')!.textContent).toContain('error');
  });

  // #463 S4 remediation round 6 — round 5 also converted `call_status` to the
  // shared set, which pinned the MIRROR of the defect it was closing as
  // intended: `classify_tool_failure` reads `completion.status` alone for the
  // mcp family, so a failing `call_status` would have rendered a card failure
  // that the Errors badge never counts and the Errors filter never surfaces.
  // The card now reads only the axis the server decides on.
  it('does not fail a card on call_status, which the server does not read', () => {
    const { container } = render(<NativeMcpCard call={call({
      schema_version: 1, type: 'mcp', source: 'function_call', name: 'fixture_tool',
      call_status: 'error',
      completion: {
        status: 'ok', server: 'fixture', tool: 'get_issue', arguments: {},
        result: { Ok: 'fine' }, duration: { secs: 0, nanos: 1_000_000 },
      },
    })} />);
    expect(container.querySelector('.conv-native-card--error')).toBeNull();
    expect(container.querySelector('.conv-outcome')!.textContent).toContain('ok');
  });

  it('leaves a wholly successful MCP completion unmarked', () => {
    const { container } = render(<NativeMcpCard call={call({
      schema_version: 1, type: 'mcp', source: 'function_call', name: 'fixture_tool',
      call_status: 'requested',
      completion: {
        status: 'ok', server: 'fixture', tool: 'get_issue', arguments: {},
        result: { Ok: 'fine' }, duration: { secs: 0, nanos: 1_000_000 },
      },
    })} />);
    expect(container.querySelector('.conv-native-card--error')).toBeNull();
    expect(container.querySelector('.conv-outcome')!.textContent).toContain('ok');
  });

  it('maps a completion fragment onto the exact rendered MCP result', () => {
    const native: NativeToolCard = {
      schema_version: 1, type: 'mcp', source: 'function_call', name: 'fixture_search', call_status: 'completed',
      completion: {
        status: 'ok', server: 'fixture', tool: 'search', arguments: { query: 'fixture' },
        result: { Ok: { content: [{ type: 'text', text: 'needle MCP' }] } },
        duration: { secs: 0, nanos: 1 }, event_block_key: 'cbk.event',
      },
    };
    const mcpCall = { ...call(native), block_key: 'cbk.call' };
    const renderedResult = JSON.stringify(native.completion.result, null, 2);
    const start = Array.from(renderedResult.slice(0, renderedResult.indexOf('needle'))).length;
    const { container } = render(
      <ExactFindContext.Provider value={{
        selectedOccurrenceId: 'occ-mcp',
        occurrences: [{
          occurrence_id: 'occ-mcp', item_key: 'item', uuid: 'item',
          block_key: 'cbk.event', container_block_key: 'cbk.call', surface: 'completion',
          match_kinds: ['tool'], disclosure: ['cbk.call'],
          fragments: [{ leaf_key: 'result', start, end: start + 6 }],
        }],
      }}>
        <NativeMcpCard call={mcpCall} />
      </ExactFindContext.Provider>,
    );
    expect(container.querySelector('.conv-code--result mark')?.textContent).toBe('needle');
    expect(container.querySelector('details')?.dataset.disclosureKey).toBe('cbk.call');
  });

  it('links only a proven child conversation using the exact opaque key', () => {
    const linked = call({
      schema_version: 1, type: 'agent', operation: 'spawn_agent', call_status: 'requested', arguments: { task_name: 'child' },
      result: { status: 'returned', value: { task_name: '/root/child' }, truncated: false },
      child_conversation: { conversation_key: 'v1.exact-child', role: 'cctally_reviewer', nickname: 'Synthetic Child' },
    });
    const { rerender } = render(<NativeAgentCard call={linked} />);
    expect(document.querySelector('.conv-outcome')?.textContent).toContain('ok');
    fireEvent.click(screen.getByRole('button', { name: /open child.*synthetic child/i }));
    expect(getState().selectedConversationRef).toEqual({ source: 'codex', key: 'v1.exact-child' });

    rerender(<NativeAgentCard call={call({
      schema_version: 1, type: 'agent', operation: 'spawn_agent', call_status: 'requested', arguments: { task_name: 'ambiguous' },
      result: { status: 'returned', value: { task_name: '/root/ambiguous' }, truncated: false },
    })} />);
    expect(screen.queryByRole('button', { name: /open child/i })).toBeNull();
  });

  // #463 S5 (F24d, spec §4.6) — `account_key` is part of conversation identity.
  // Rebuilding the child ref without it opened an accountless identity that no
  // rail row compared as current while the global chip still named an account.
  it('#463 S5 — carries the current conversation account into the child ref', () => {
    const linked = call({
      schema_version: 1, type: 'agent', operation: 'spawn_agent', call_status: 'requested', arguments: { task_name: 'child' },
      result: { status: 'returned', value: { task_name: '/root/child' }, truncated: false },
      child_conversation: { conversation_key: 'v1.exact-child', role: 'cctally_reviewer', nickname: 'Synthetic Child' },
    });
    render(
      <TranscriptContext.Provider value={{
        sessionId: null,
        conversationRef: { source: 'codex', key: 'v1.parent', account_key: 'acct-1' },
      }}>
        <NativeAgentCard call={linked} />
      </TranscriptContext.Provider>,
    );
    fireEvent.click(screen.getByRole('button', { name: /open child.*synthetic child/i }));
    expect(getState().selectedConversationRef).toEqual({
      source: 'codex', key: 'v1.exact-child', account_key: 'acct-1',
    });
  });

  it('#463 S5 — does not stamp a Claude account onto a Codex child ref', () => {
    const linked = call({
      schema_version: 1, type: 'agent', operation: 'spawn_agent', call_status: 'requested', arguments: { task_name: 'child' },
      result: { status: 'returned', value: { task_name: '/root/child' }, truncated: false },
      child_conversation: { conversation_key: 'v1.exact-child', role: 'cctally_reviewer', nickname: 'Synthetic Child' },
    });
    render(
      <TranscriptContext.Provider value={{
        sessionId: null,
        conversationRef: { source: 'claude', key: 'SID-A', account_key: 'claude-acct' },
      }}>
        <NativeAgentCard call={linked} />
      </TranscriptContext.Provider>,
    );
    fireEvent.click(screen.getByRole('button', { name: /open child.*synthetic child/i }));
    expect(getState().selectedConversationRef).toEqual({ source: 'codex', key: 'v1.exact-child' });
  });
});
