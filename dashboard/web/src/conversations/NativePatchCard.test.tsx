import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { NativePatchCard } from './NativePatchCard';
import type { ConversationBlock, NativePatchFile } from '../types/conversation';
import { ExactFindContext } from './HighlightContext';

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
  it('maps a completion fragment onto the exact rendered diff row', () => {
    const patchCall = call({ block_key: 'cbk.patch' });
    const { container } = render(
      <ExactFindContext.Provider value={{
        selectedOccurrenceId: 'occ-patch',
        occurrences: [{
          occurrence_id: 'occ-patch', item_key: 'item', uuid: 'item',
          block_key: 'cbk.event', container_block_key: 'cbk.patch', surface: 'completion',
          match_kinds: ['tool'], disclosure: ['cbk.patch'],
          fragments: [{ leaf_key: 'files.0.diff.0.1', start: 0, end: 3 }],
        }],
      }}>
        <NativePatchCard call={patchCall} />
      </ExactFindContext.Provider>,
    );
    const mark = container.querySelector('.conv-diff-row--add mark');
    expect(mark?.textContent).toBe('new');
    expect(mark?.getAttribute('data-find-occurrence-id')).toBe('occ-patch');
    expect(container.querySelector('details')?.dataset.disclosureKey).toBe('cbk.patch');
  });

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
    expect(container.textContent).toContain('This change carries no line-level diff');
    expect(container.textContent).toContain('modified');
    expect(container.textContent).toContain('synthetic-summary.txt');
    expect(container.querySelector('.conv-term-stderr')?.textContent).toContain('synthetic failure');
    expect(container.querySelector('.conv-term-badge--err')).toBeTruthy();
    expect(container.querySelector('button[aria-label="Load raw event payload"]')).toBeTruthy();
  });
});

// #463 S3 §3.1 — per-file truncation and diff provenance. Card-level
// `truncated` means the card as a whole was cut; per-file `truncated` means
// THAT file's own text was cut. They are independent.
describe('NativePatchCard — per-file truncation and diff provenance', () => {
  const eventCall = (files: NativePatchFile[]): Call => call({
    name: 'patch_apply_end', payload_kind: 'event',
    native_card: {
      schema_version: 1, type: 'patch', source: 'patch_apply_end', status: 'completed',
      success: true, has_diff: true, stdout: '', stderr: '', truncated: false, files,
    },
  });

  it('marks a clipped file on the file, not on the card', () => {
    const { container } = render(<NativePatchCard call={eventCall([
      { path: '/s/big.py', status: 'delete', truncated: true, diff_source: 'derived',
        unified_diff: '--- /s/big.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n' },
      { path: '/s/small.py', status: 'add', truncated: false, diff_source: 'derived',
        unified_diff: '--- /dev/null\n+++ /s/small.py\n@@ -0,0 +1,1 @@\n+kept\n' },
    ])} />);
    const files = container.querySelectorAll('.conv-native-patch-file');
    expect(files[0].querySelector('.conv-native-patch-fileclipped')).toBeTruthy();
    expect(files[1].querySelector('.conv-native-patch-fileclipped')).toBeNull();
    expect(container.querySelector('.conv-native-patch-truncated')).toBeNull();
  });

  it('says a clipped-to-nothing diff exists rather than claiming none was retained', () => {
    // Wire contract §4: `truncated: true` with no `unified_diff` means "there
    // is a diff and none of it survived the budget".
    const { container } = render(<NativePatchCard call={eventCall([
      { path: '/s/minified.js', status: 'update', truncated: true, diff_source: 'retained' },
    ])} />);
    expect(container.textContent).not.toContain('carries no line-level diff');
    expect(container.querySelector('.conv-native-patch-nodiff')?.textContent)
      .toContain('none of it fit');
  });

  it('claims only what it knows when the change genuinely has no diff', () => {
    // "No diff retained" reads as a statement about the provider's retention.
    // What the card actually knows is narrower: this change carries no
    // line-level diff, whoever decided that.
    const { container } = render(<NativePatchCard call={eventCall([
      { path: '/s/plain.txt', status: 'update', truncated: false },
    ])} />);
    expect(container.querySelector('.conv-native-patch-nodiff')?.textContent)
      .toContain('This change carries no line-level diff');
  });

  it('labels a derived diff quietly rather than presenting it as provider-supplied', () => {
    const { container } = render(<NativePatchCard call={eventCall([
      { path: '/s/added.py', status: 'add', truncated: false, diff_source: 'derived',
        unified_diff: '--- /dev/null\n+++ /s/added.py\n@@ -0,0 +1,1 @@\n+one\n' },
      { path: '/s/updated.py', status: 'update', truncated: false, diff_source: 'retained',
        unified_diff: '@@ -1 +1 @@\n-old\n+new\n' },
    ])} />);
    const files = container.querySelectorAll('.conv-native-patch-file');
    expect(files[0].querySelector('.conv-native-patch-derived')?.textContent)
      .toContain('rendered from retained content');
    expect(files[1].querySelector('.conv-native-patch-derived')).toBeNull();
  });

  it('treats an absent per-file truncated as false', () => {
    // A pre-S3 entry carries no `truncated` key at all.
    const { container } = render(<NativePatchCard call={eventCall([
      { path: '/s/legacy.py', status: 'update',
        unified_diff: '@@ -1 +1 @@\n-old\n+new\n' },
    ])} />);
    expect(container.querySelector('.conv-native-patch-fileclipped')).toBeNull();
  });
});

// #463 S4 remediation round 4 (P2-2) — the card's own failure test was a bare
// `status === 'failed'` literal, the same one the adapter carried, so a patch
// event whose provider status is the word `error` rendered with no failure
// treatment while the server had already published a `tool_error` landmark for
// it and the Errors badge had already counted it.
describe('NativePatchCard failure verdict (#463 S4)', () => {
  const patch = (over: Record<string, unknown>) => call({
    name: 'patch_apply_end', payload_kind: 'event',
    result: { text: '', truncated: false, is_error: false },
    native_card: {
      schema_version: 1, type: 'patch', source: 'patch_apply_end',
      has_diff: false, stdout: '', stderr: '', truncated: false,
      files: [{ path: 'a.py', status: 'update' }],
      status: 'completed', success: null, ...over,
    },
  } as Partial<Call>);

  it('marks a patch whose status is the word `error` as failed', () => {
    const { container } = render(<NativePatchCard call={patch({ status: 'error' })} />);
    expect(container.querySelector('.conv-term-badge--err')).not.toBeNull();
  });

  it('still marks `failed` as failed', () => {
    const { container } = render(<NativePatchCard call={patch({ status: 'failed' })} />);
    expect(container.querySelector('.conv-term-badge--err')).not.toBeNull();
  });

  it('does not mark a completed patch as failed', () => {
    const { container } = render(<NativePatchCard call={patch({ status: 'completed', success: true })} />);
    expect(container.querySelector('.conv-term-badge--err')).toBeNull();
  });
});
