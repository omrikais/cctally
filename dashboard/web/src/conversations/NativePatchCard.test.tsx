import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { NativePatchCard } from './NativePatchCard';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

const call = (over: Partial<Call> = {}): Call => ({
  kind: 'tool_call', name: 'apply_patch', input_summary: 'patch', input: null,
  preview: 'src/a.ts', tool_use_id: 'cbk.patch', payload_capable: true,
  result: { text: 'Done!', truncated: false, is_error: false },
  native_card: {
    schema_version: 1, type: 'patch', source: 'apply_patch', status: 'completed', success: true,
    has_diff: true, stdout: 'Done!', stderr: '', truncated: false,
    files: [
      { path: 'src/a.ts', status: 'modified', unified_diff: '--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-old\n+new\n' },
      { path: 'src/old.ts', move_path: 'src/new.ts', status: 'moved', unified_diff: '--- a/src/old.ts\n+++ b/src/new.ts\n' },
    ],
    event_payload_key: 'cbk.patch-event',
  },
  ...over,
} as Call);

describe('NativePatchCard', () => {
  it('renders exact retained hunks and truthful file/move labels through shared diff primitives', () => {
    const { container } = render(<NativePatchCard call={call()} />);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('apply_patch');
    expect(container.querySelector('.conv-diff-row--del')?.textContent).toContain('old');
    expect(container.querySelector('.conv-diff-row--add')?.textContent).toContain('new');
    expect(container.textContent).toContain('src/a.ts');
    expect(container.textContent).toContain('src/old.ts → src/new.ts');
    expect(container.textContent).toContain('Retained diff may not be directly applicable');
    expect(container.querySelectorAll('.conv-native-patch-file')).toHaveLength(2);
    expect(container.querySelector('button[aria-label="Load raw request payload"]')).toBeTruthy();
    expect(container.querySelector('button[aria-label="Load raw event payload"]')).toBeTruthy();
  });

  it('renders an honest path/status summary and failure stream when no diff was retained', () => {
    const { container } = render(<NativePatchCard call={call({
      name: 'patch_apply_end',
      payload_kind: 'event',
      result: { text: 'synthetic failure', truncated: false, is_error: true },
      native_card: {
        schema_version: 1, type: 'patch', source: 'patch_apply_end', status: 'failed', success: false,
        has_diff: false, stdout: '', stderr: 'synthetic failure', truncated: false,
        files: [{ path: 'synthetic-summary.txt', status: 'modified' }],
      },
    })} />);

    expect(container.querySelector('.conv-diff-hunk')).toBeNull();
    expect(container.textContent).toContain('No diff retained');
    expect(container.textContent).toContain('modified');
    expect(container.textContent).toContain('synthetic-summary.txt');
    expect(container.querySelector('.conv-term-stderr')?.textContent).toContain('synthetic failure');
    expect(container.querySelector('.conv-term-badge--err')).toBeTruthy();
    expect(container.querySelector('button[aria-label="Load raw event payload"]')).toBeTruthy();
  });
});
