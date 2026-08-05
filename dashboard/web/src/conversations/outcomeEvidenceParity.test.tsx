import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { BashCard } from './BashCard';
import { MessageBlocks } from './MessageBlocks';
import { NativePatchCard } from './NativePatchCard';
import { NativeProgramCard } from './NativeProgramCard';
import { NativeMcpCard } from './NativeSecondaryToolCards';
import { SessionRefCard } from './SessionRefCard';
import { TranscriptContext } from './TranscriptContext';
import { WebSearchCard } from './WebSearchCard';
import type { ConversationBlock, ToolOutcome } from '../types/conversation';

// #463 S3 F11a — the recovered exit code and wall time reached the reader
// through a `title` attribute alone, which a touch viewport cannot open. Three
// of the card families that render an OutcomeBadge then grew an evidence
// line in their expanded body and two did not, which no per-component test
// could see: each family's own test file asserts only its own component. This
// file states the invariant across families and pins it to the SOURCE, so the
// next family to render a badge cannot silently omit the line.

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

const EVIDENCE: ToolOutcome = { status: 'completed', exit_code: 0, wall_time_seconds: 1.5 };
const EVIDENCE_TEXT = 'exit 0 · 1.5s';

function withTranscript(node: React.ReactElement) {
  return render(
    <TranscriptContext.Provider value={{ sessionId: 's1' }}>{node}</TranscriptContext.Provider>,
  );
}

// One entry per card family that can render an OutcomeBadge. `module` names the
// source file the family lives in; the source scan below requires every module
// that renders a badge to appear here, so a NEW family fails this file rather
// than shipping without an evidence line.
const FAMILIES: {
  name: string;
  module: string;
  render: (outcome: ToolOutcome) => HTMLElement;
}[] = [
  {
    name: 'BashCard (conv-term)',
    module: 'conversations/BashCard.tsx',
    render: (outcome) => withTranscript(<BashCard call={{
      kind: 'tool_call', name: 'exec_command', input_summary: '{}', preview: 'ls',
      tool_use_id: 'oe-term', input: { command: 'ls -la' }, stderr: null, interrupted: false,
      result: { text: 'ok\n', truncated: false, is_error: false }, outcome,
    } as Call} />).container,
  },
  {
    name: 'NativeProgramCard (conv-program)',
    module: 'conversations/NativeProgramCard.tsx',
    render: (outcome) => render(<NativeProgramCard call={{
      kind: 'tool_call', name: 'exec', input_summary: 'program', input: null,
      preview: 'ls -1', tool_use_id: 'oe-program', payload_capable: true,
      result: { text: 'ok\n', truncated: false, is_error: false }, outcome,
      native_card: {
        schema_version: 1, type: 'program', title: null, complete: false, truncated: false,
        invocations: [{ kind: 'command', command: 'ls -1', workdir: '/synthetic', metadata: {} }],
      },
    } as Call} />).container,
  },
  {
    name: 'SessionRefCard (conv-session-ref-card)',
    module: 'conversations/SessionRefCard.tsx',
    render: (outcome) => render(<SessionRefCard call={{
      kind: 'tool_call', name: 'write_stdin', input_summary: '{}', input: null,
      preview: 'yes', tool_use_id: 'oe-session', payload_capable: true,
      result: { text: 'ok\n', truncated: false, is_error: false }, outcome,
      native_card: {
        schema_version: 1, type: 'session_ref', scope: 'shell', ref: '1',
        operation: 'write', chars: 'yes\n', truncated: false,
      },
    } as Call} />).container,
  },
  {
    name: 'NativePatchCard (conv-native-patch)',
    module: 'conversations/NativePatchCard.tsx',
    render: (outcome) => render(<NativePatchCard call={{
      kind: 'tool_call', name: 'apply_patch', input_summary: 'patch', input: null,
      preview: 'src/a.ts', tool_use_id: 'oe-patch', payload_capable: true,
      result: { text: 'Done!', truncated: false, is_error: false }, outcome,
      native_card: {
        schema_version: 1, type: 'patch', source: 'apply_patch', status: 'completed',
        success: true, has_diff: true, stdout: 'Done!', stderr: '', truncated: false,
        files: [{
          path: 'src/a.ts', status: 'modified',
          unified_diff: '--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-old\n+new\n',
        }],
      },
    } as Call} />).container,
  },
  {
    name: 'MessageBlocks generic chip (conv-chip--tool)',
    module: 'conversations/MessageBlocks.tsx',
    render: (outcome) => render(<MessageBlocks blocks={[{
      kind: 'tool_call', name: 'browser_click', input_summary: '{}', preview: '/a',
      tool_use_id: 'oe-generic',
      result: { text: 'A', truncated: false, is_error: false }, outcome,
    } as Call]} />).container,
  },
  {
    name: 'NativeMcpCard (conv-native-mcp)',
    module: 'conversations/NativeSecondaryToolCards.tsx',
    render: (outcome) => render(<NativeMcpCard call={{
      kind: 'tool_call', name: 'fixture_tool', input_summary: '{}', input: {},
      preview: 'fixture', tool_use_id: 'oe-mcp', result: { text: 'ok', truncated: false, is_error: false }, outcome,
      native_card: {
        schema_version: 1, type: 'mcp', source: 'function_call', name: 'fixture_tool', call_status: 'requested',
        completion: {
          status: 'ok', server: 'fixture', tool: 'get_issue', arguments: {}, result: { Ok: 'fine' },
          duration: { secs: 0, nanos: 1_000_000 },
        },
      },
    } as Call} />).container,
  },
  {
    name: 'WebSearchCard (conv-web)',
    module: 'conversations/WebSearchCard.tsx',
    render: (outcome) => render(<WebSearchCard call={{
      kind: 'tool_call', name: 'WebSearch', input_summary: '{}', input: { query: 'fixture' },
      preview: 'fixture', tool_use_id: 'oe-web', result: { text: '', truncated: false, is_error: false }, outcome,
      web_search: { query: 'fixture', links: [] },
      native_card: {
        schema_version: 1, type: 'web_search', source: 'web_search_call', call_status: 'completed',
        query: 'fixture', action: {},
        completion: { status: 'returned', query: 'fixture', action: {}, results: [] },
      },
    } as Call} />).container,
  },
];

describe('outcome evidence parity across card families (#463 S3 F11a)', () => {
  it.each(FAMILIES.map((family) => [family.name, family] as const))(
    '%s states the recovered evidence in its expanded body',
    (_name, family) => {
      const container = family.render(EVIDENCE);
      // Anti-vacuity: the fixture must actually reach the badge, otherwise the
      // evidence assertion below would be testing a card that renders neither.
      expect(container.querySelector('.conv-outcome'), 'fixture rendered no outcome badge').toBeTruthy();
      const line = container.querySelector('.conv-outcome-evidence');
      expect(line?.textContent).toBe(EVIDENCE_TEXT);
      // In the BODY, not the collapsed row — the row is where the evidence was
      // already unreachable, and a summary-only line would re-create F11a.
      expect(line?.closest('summary'), 'evidence line rendered inside the summary row').toBeNull();
    },
  );

  it('renders no evidence line for a call whose grammar recovered neither value', () => {
    for (const family of FAMILIES) {
      const container = family.render({ status: 'completed', exit_code: null, wall_time_seconds: null });
      expect(container.querySelector('.conv-outcome-evidence'), family.name).toBeNull();
    }
  });
});

// The render assertions above cover the families this file enumerates. These two
// read the source tree instead, so a family added tomorrow — in this directory
// or any other — fails here rather than shipping a badge with no evidence.
const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..');

function tsxSources(dir: string): { module: string; src: string }[] {
  const out: { module: string; src: string }[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      out.push(...tsxSources(path));
    } else if (entry.endsWith('.tsx') && !entry.endsWith('.test.tsx')) {
      out.push({ module: relative(SRC_DIR, path), src: readFileSync(path, 'utf8') });
    }
  }
  return out;
}

function badgeRenderers(): string[] {
  return tsxSources(SRC_DIR)
    .filter(({ module, src }) => module !== 'conversations/OutcomeBadge.tsx' && src.includes('<OutcomeBadge'))
    .map(({ module }) => module)
    .sort();
}

describe('outcome evidence parity — source scan (#463 S3 F11a)', () => {
  it('finds every card family that renders an OutcomeBadge', () => {
    // Anti-vacuity anchor: a scan that matched nothing would pass both
    // assertions below while observing nothing at all.
    expect(badgeRenderers()).toEqual([...FAMILIES].map((f) => f.module).sort());
  });

  it('requires every module that renders an OutcomeBadge to render an OutcomeEvidence', () => {
    const sources = new Map(tsxSources(SRC_DIR).map(({ module, src }) => [module, src]));
    const missing = badgeRenderers().filter((module) => !sources.get(module)!.includes('<OutcomeEvidence'));
    expect(missing).toEqual([]);
  });
});
