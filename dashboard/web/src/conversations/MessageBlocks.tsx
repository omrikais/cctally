import { useState, type ReactNode } from 'react';
import { Markdown } from '../components/Markdown';
import {
  toolIcon,
  ThinkingIcon,
  ResultIcon,
  ReferenceIcon,
  SystemIcon,
} from './ConvIcons';
import { CopyButton } from './CopyButton';
import { highlightBody } from './CodeBlock';
import { splitToReactNodes, useFindSplit } from './findMark';
import { LineNumberedCode } from './LineNumberedCode';
import { resultLang } from './toolLang';
import { specialToolRenderer } from './specialTools';
import { TaskChecklistCard } from './TaskChecklistCard';
import { parseMcpName } from './parseMcpName';
import { MediaFigure } from './MediaFigure';
import { LoadFull } from './LoadFull';
import { useFocusMode } from './TranscriptContext';
import { NativePayloadDisclosure } from './NativePayloadDisclosure';
import type { ConversationBlock } from '../types/conversation';

// #177 S4 (Q5-A): MCP chips show `action [server-pill]`; the full original
// name stays in the title tooltip + expanded request panel. Non-MCP names
// render EXACTLY as before (byte-identical — pinned by test).
function ChipName({ name }: { name: string | null | undefined }) {
  const mcp = parseMcpName(name);
  if (!mcp) return <span className="conv-chip-name">{name ?? 'tool'}</span>;
  return (
    <>
      <span className="conv-chip-name" title={name ?? undefined}>{mcp.action}</span>
      <span className="conv-chip-server">{mcp.serverLabel}</span>
    </>
  );
}

// #228 S2 (A3) — the spawn→agent connector that replaces a suppressed spawn
// chip, making the spawn→work flow explicit.
function SpawnConnector({ kind }: { kind: string }) {
  return (
    <div className="conv-spawn-connector">
      <span className="conv-spawn-connector-arc" aria-hidden="true">↳</span>
      <span>{kind ? `launched ${kind} agent` : 'launched agent'}</span>
    </div>
  );
}

type ToolCall = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Claude Code's live to-do family. A run of these whose FIRST call carries a
// kernel-stamped task_snapshot collapses to ONE checklist card (see
// isTaskChecklistRun); anything else stays the generic tool-run group.
const TASK_TRIO = new Set(['TaskCreate', 'TaskUpdate', 'TaskList']);

// A Task* checklist run = the run's first call is a Task* tool AND carries a
// task_snapshot array. The kernel stamps the snapshot on the run's first call
// only, so checking the first call is sufficient and avoids mis-collapsing a
// run that merely interleaves a Task* call after other tools.
function isTaskChecklistRun(calls: ToolCall[]): boolean {
  const first = calls[0];
  return (
    first != null &&
    first.name != null &&
    TASK_TRIO.has(first.name) &&
    Array.isArray(first.task_snapshot)
  );
}

// Render a turn's blocks in DOCUMENT ORDER (#164): consecutive `text` coalesce
// into one <Markdown>; a maximal run of consecutive `tool_call` becomes one
// tool-run group (head only when N>=2); `thinking` is its own chip; media /
// references are inline placeholder spans (and terminate a tool-run). Every
// disclosure is a native <details> with a chevron. `tool_use` (id-less
// degradation) and `tool_result` (orphan item only) render as single chips too.
// This single source of truth is used by both the assistant turn (which renders
// its prose-from-text-blocks here, in order) and the human turn.
export function MessageBlocks({ blocks, anchorUuid, suppressToolUseIds, spawnKindByToolUseId }: {
  blocks: ConversationBlock[];
  anchorUuid?: string | null;
  // §5 (Codex P1-C) — the set of spawn `tool_use_id`s whose nested subagent card
  // is the canonical representation. A `tool_call` block whose `tool_use_id` is
  // in this set is dropped from the walk (its card renders the spawn). Granular
  // by `tool_use_id`, NOT name/item, because one assistant item can hold several
  // spawns; an unresolved spawn (no nested card, e.g. >16 KB clip) is NOT in the
  // set so its chip still renders.
  suppressToolUseIds?: Set<string>;
  // #228 S2 (A3) — tool_use_id → subagent kind for spawns whose card IS loaded
  // (built from flattenSubagents, so the map omits paged-out spawns). A
  // suppressed spawn in this map renders a "↳ launched <kind> agent" connector
  // IN PLACE of the dropped chip; a suppressed spawn NOT in the map (paged out)
  // renders nothing — connector ⟺ card present.
  spawnKindByToolUseId?: Map<string, string>;
}) {
  // #177 S5 — chat focus mode strips tool/orphan-result texture so a turn reads
  // as prose-only conversation. text + thinking render unchanged; tool_call /
  // tool_use runs and orphan tool_result chips are dropped from the walk.
  const chat = useFocusMode() === 'chat';
  const out: ReactNode[] = [];
  let i = 0;
  let textRun: string[] = [];
  const flushText = () => {
    if (textRun.length) {
      // Coalesced text fragments rejoin with a blank line so adjacent prose
      // paragraphs stay distinct in the rendered Markdown.
      out.push(<Markdown key={`t${out.length}`}>{textRun.join('\n\n')}</Markdown>);
      textRun = [];
    }
  };
  while (i < blocks.length) {
    const b = blocks[i];
    if (b.kind === 'text') {
      textRun.push(b.text);
      i++;
      continue;
    }
    flushText();
    if (b.kind === 'tool_call') {
      let run: Extract<ConversationBlock, { kind: 'tool_call' }>[] = [];
      const flushRun = () => {
        // chat mode suppresses tool runs entirely (prose only).
        if (!chat && run.length) out.push(<ToolRun key={`r${out.length}`} calls={run} />);
        run = [];
      };
      while (i < blocks.length && blocks[i].kind === 'tool_call') {
        const tc = blocks[i] as Extract<ConversationBlock, { kind: 'tool_call' }>;
        const suppressed = tc.tool_use_id != null && suppressToolUseIds?.has(tc.tool_use_id);
        if (suppressed) {
          // §5 — the spawn's nested card is canonical, so the chip is dropped.
          // #228 S2 (A3) — if the card is LOADED (in the kind map), emit a
          // connector in document position; flush the current run first so
          // [tool, spawn, spawn, tool] renders in order, not connectors-after-run.
          const kind = tc.tool_use_id != null ? spawnKindByToolUseId?.get(tc.tool_use_id) : undefined;
          if (!chat && kind !== undefined) {
            flushRun();
            out.push(<SpawnConnector key={`sc-${tc.tool_use_id}`} kind={kind} />);
          }
          // else: paged-out spawn (suppressed, not loaded) → render nothing.
        } else {
          run.push(tc);
        }
        i++;
      }
      flushRun();
      continue;
    }
    // chat mode suppresses the tool_use degradation chip + orphan tool_result
    // chips (the rest — thinking / media / references — survive).
    if (chat && (b.kind === 'tool_use' || b.kind === 'tool_result')) {
      i++;
      continue;
    }
    out.push(<BlockChip key={`c${out.length}`} block={b} anchorUuid={anchorUuid} />);
    i++;
  }
  flushText();
  if (out.length === 0) return null;
  return <div className="conv-blocks">{out}</div>;
}

// A maximal run of consecutive tool_call blocks. A run of N>=2 gets a
// "tool run · N actions" head (label + trailing rule via CSS); a single call
// renders a bare chip with no head.
function ToolRun({ calls }: { calls: Extract<ConversationBlock, { kind: 'tool_call' }>[] }) {
  // A Task* checklist run collapses its LEADING Task* sub-run to ONE card
  // showing the running to-do list snapshot (the kernel folds the whole run's
  // ops into the first call's task_snapshot), suppressing those N generic chips
  // + the "tool run · N actions" head. #245: only the leading Task* sub-run is
  // collapsed — any trailing tool calls (a Codex error, a generic chip, …) still
  // render through the normal chip path instead of being discarded.
  let checklist: ReactNode = null;
  let rest = calls;
  if (isTaskChecklistRun(calls)) {
    let lead = 1;
    while (lead < calls.length && calls[lead].name != null && TASK_TRIO.has(calls[lead].name!)) {
      lead++;
    }
    checklist = <TaskChecklistCard call={calls[0]} />;
    rest = calls.slice(lead);
  }
  return (
    <div className="conv-toolrun">
      {checklist}
      {rest.length >= 2 && (
        <div className="conv-toolrun-head">tool run · {rest.length} actions</div>
      )}
      {rest.map((c, i) => (
        <ToolCallChip key={i} call={c} />
      ))}
    </div>
  );
}

type ToolResult = { text: string; truncated: boolean; is_error: boolean };

// Pick the RESULT renderer: a non-error Read whose file resolves to a known
// language → gutter + highlight; everything else → the existing plain pre.
function ToolResultBody({ result, name, preview }: { result: ToolResult; name: string | null; preview: string }) {
  const split = useFindSplit();
  const lang = name === 'Read' && !result.is_error ? resultLang('Read', preview) : '';
  if (lang) return <LineNumberedCode code={result.text} lang={lang} />;
  // #236 — generic result <pre> is highlight-aware (find-closed → bare text).
  return (
    <pre className="conv-code conv-code--result">
      {split ? splitToReactNodes(result.text, split) : result.text}
    </pre>
  );
}

// One paired request+result disclosure. Collapsed: chevron · tool icon · name ·
// one-line preview · status (· error / · truncated). Expanded: the request
// (input_summary) plus the result body (result.text, scroll-capped) or a
// "no result" note when the request was never matched (result === null).
//
// Skill-content nesting: when the kernel folded an injected skill body into this
// Skill chip (skill_body != null), the chip expands straight to the rich-markdown
// body — NO request/result panels (the trivial "Launching skill" result was
// dropped; args are a poor fidelity carrier). Header is identical to the
// collapsed look the user already sees, so the chip simply becomes the thing
// that expands. Collapsed by default.
function ToolCallChip({ call }: { call: Extract<ConversationBlock, { kind: 'tool_call' }> }) {
  const split = useFindSplit();
  const [fullInput, setFullInput] = useState<string | null>(null);
  const [fullResult, setFullResult] = useState<string | null>(null);
  if (call.skill_body != null) {
    return (
      <details className="conv-chip conv-chip--tool conv-chip--skill">
        <summary>
          <span className="conv-chev" aria-hidden="true" />
          {toolIcon(call.name)} <ChipName name={call.name} />
          <span className="conv-chip-preview">{call.preview}</span>
        </summary>
        <div className="conv-chip-body">
          <CopyButton text={call.skill_body} />
          <Markdown>{call.skill_body}</Markdown>
        </div>
      </details>
    );
  }
  const special = specialToolRenderer(call);
  if (special) return special;
  const status = call.result?.is_error
    ? ' · error'
    : call.result?.truncated
      ? ' · truncated'
      : '';
  return (
    <details className="conv-chip conv-chip--tool">
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        {toolIcon(call.name)} <ChipName name={call.name} />
        <span className="conv-chip-preview">{call.preview}</span>
        {status && <span className="conv-chip-status">{status}</span>}
      </summary>
      <div className="conv-chip-body conv-chip-body--io">
        <div className="conv-tool-io">
          <div className="conv-tool-io-label">request</div>
          <CopyButton text={fullInput ?? call.input_summary} />
          <pre className="conv-code conv-code--hl">{highlightBody(fullInput ?? call.input_summary, 'json', split)}</pre>
          {call.payload_capable && call.tool_use_id && fullInput == null && (
            <LoadFull
              toolUseId={call.tool_use_id}
              which="input"
              fullLength={null}
              label="load full request"
              onLoaded={(payload) => {
                if (payload.which === 'input') setFullInput(JSON.stringify(payload.input, null, 2));
              }}
            />
          )}
        </div>
        {call.result ? (
          <div className="conv-tool-io">
            <div className="conv-tool-io-label">
              result{call.result.is_error ? ' · error' : ' · ok'}
              {call.result.truncated ? ' · truncated' : ''}
            </div>
            <CopyButton text={fullResult ?? call.result.text} />
            <ToolResultBody result={{ ...call.result, text: fullResult ?? call.result.text }} name={call.name} preview={call.preview} />
            {call.payload_capable && call.tool_use_id && fullResult == null && (
              <LoadFull
                toolUseId={call.tool_use_id}
                which="result"
                fullLength={null}
                label="load full result"
                onLoaded={(payload) => {
                  if (payload.which === 'result') setFullResult(payload.text);
                }}
              />
            )}
            {/* #177 S4 (Q7-A): tool-result screenshots render inline after the
                text panel, in document order, addressed by this call's id. */}
            {call.result.media?.map((m) => (
              <MediaFigure key={m.index} media={m} toolUseId={call.tool_use_id} context={call.name ?? 'tool'} />
            ))}
          </div>
        ) : (
          <div className="conv-tool-io">
            <div className="conv-tool-io-label conv-tool-io-label--none">no result</div>
          </div>
        )}
      </div>
    </details>
  );
}

// First non-blank line of a block's text, trimmed + capped, for a collapsed
// chip's one-line preview.
function firstLine(s: string): string {
  const t = s.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  return t.length > 80 ? `${t.slice(0, 80).trimEnd()}…` : t;
}

const GIT_ACTION_LABELS = {
  create_branch: 'Branch created',
  stage: 'Changes staged',
  commit: 'Commit created',
  push: 'Branch pushed',
  create_pr: 'Pull request created',
} as const;

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

function CodexReasoningBlock({ block }: {
  block: Extract<ConversationBlock, { kind: 'codex_reasoning' }>;
}) {
  const headline = block.title ?? block.summary ?? block.body ?? '';
  const expandable = block.body != null;
  const summary = (
    <>
      {expandable && <span className="conv-chev" aria-hidden="true" />}
      <ThinkingIcon />
      <span className="conv-codex-reasoning-label">Reasoning</span>
      <span className="conv-codex-reasoning-title"><Markdown>{headline}</Markdown></span>
    </>
  );
  if (!expandable) {
    return <div className="conv-codex-reasoning conv-codex-reasoning--line" role="note">{summary}</div>;
  }
  return (
    <details className="conv-codex-reasoning conv-codex-reasoning--expandable">
      <summary>{summary}</summary>
      <div className="conv-codex-reasoning-content">
        {block.summary && (
          <div className="conv-codex-reasoning-summary">
            <span>Summary</span><Markdown>{block.summary}</Markdown>
          </div>
        )}
        <div className="conv-codex-reasoning-body">
          <span>Body</span><Markdown>{block.body ?? ''}</Markdown>
        </div>
      </div>
    </details>
  );
}

function SystemActionsBlock({ block }: {
  block: Extract<ConversationBlock, { kind: 'system_actions' }>;
}) {
  return (
    <div className="conv-system-actions" role="note" aria-label="System actions">
      <span className="conv-system-actions-label"><SystemIcon /> System actions</span>
      <span className="conv-system-actions-list">
        {block.actions.map((action, index) => action.type === 'git' ? (
          <span className="conv-system-action" key={`${action.type}-${action.action}-${index}`}>
            {GIT_ACTION_LABELS[action.action]}{action.action === 'create_pr' && action.draft ? ' · draft' : ''}
          </span>
        ) : (
          <span className="conv-system-action" key={`${action.type}-${index}`}>
            Memory references attached · {plural(action.citation_count, 'citation')} · {plural(action.rollout_count, 'rollout')}
          </span>
        ))}
      </span>
      {block.payload_key && <NativePayloadDisclosure blockKey={block.payload_key} which="event" label="event" />}
    </div>
  );
}

function CodexLifecycleBlock({ block }: {
  block: Extract<ConversationBlock, { kind: 'codex_lifecycle' }>;
}) {
  const label = block.event === 'task_started' ? 'Codex task started' : 'Codex task complete';
  return (
    <div className={`conv-codex-lifecycle${block.error ? ' is-error' : ''}`} role="note">
      <div className="conv-codex-lifecycle-head"><SystemIcon /> <strong>{label}</strong></div>
      {block.message && <div className="conv-codex-lifecycle-message">{block.message}</div>}
      {block.error && <div className="conv-codex-lifecycle-error">{block.error}</div>}
      {block.duration_ms != null && <div className="conv-codex-lifecycle-duration">{(block.duration_ms / 1000).toFixed(1)}s</div>}
      {block.payload_key && <NativePayloadDisclosure blockKey={block.payload_key} which="event" label="event" />}
    </div>
  );
}

// Single non-text, non-tool_call block: thinking chip, the tool_use degradation
// fallback, an orphan tool_result chip, or an inline media/reference span.
function BlockChip({ block, anchorUuid }: { block: ConversationBlock; anchorUuid?: string | null }) {
  const split = useFindSplit();
  switch (block.kind) {
    case 'codex_reasoning':
      return <CodexReasoningBlock block={block} />;
    case 'system_actions':
      return <SystemActionsBlock block={block} />;
    case 'codex_lifecycle':
      return <CodexLifecycleBlock block={block} />;
    case 'thinking':
      return (
        <details className="conv-chip conv-chip--thinking">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            <ThinkingIcon /> <span className="conv-chip-name">Thinking</span>
            <span className="conv-chip-preview">{firstLine(block.text)}</span>
          </summary>
          <div className="conv-chip-body">
            <Markdown>{block.text}</Markdown>
          </div>
        </details>
      );
    case 'tool_use': // degradation only (id-less pre-migration rows)
      return (
        <details className="conv-chip conv-chip--tool">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            {toolIcon(block.name)} <ChipName name={block.name} />
          </summary>
          <div className="conv-chip-body conv-tool-io">
            <CopyButton text={block.input_summary} />
            <pre className="conv-code">{split ? splitToReactNodes(block.input_summary, split) : block.input_summary}</pre>
          </div>
        </details>
      );
    case 'tool_result': // orphan items only
      return (
        <details className="conv-chip conv-chip--result">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            <ResultIcon /> <span className="conv-chip-name">Result</span>
            <span className="conv-chip-preview">{firstLine(block.text)}</span>
            {block.is_error && <span className="conv-chip-status"> · error</span>}
            {block.truncated && <span className="conv-chip-status"> · truncated</span>}
          </summary>
          <div className="conv-chip-body conv-tool-io">
            <CopyButton text={block.text} />
            <pre className="conv-code">{split ? splitToReactNodes(block.text, split) : block.text}</pre>
            {/* #177 S4: orphaned tool-result screenshots still render — the
                kernel keeps `media` + `tool_use_id` on the standalone block. */}
            {block.media?.map((m) => (
              <MediaFigure key={m.index} media={m} toolUseId={block.tool_use_id} context="tool result" />
            ))}
          </div>
        </details>
      );
    case 'image':
    case 'document':
      // #177 S4 (Q7-A): inline figure (image) / upgraded open-link badge
      // (document) via the uuid-mode media route; degrades to the byte-count
      // badge when unaddressable (pre-reingest rows / null anchor).
      return (
        <MediaFigure
          media={{ kind: block.kind, media_type: block.media_type, bytes: block.bytes, index: block.index ?? -1 }}
          uuid={anchorUuid}
          context="attached"
        />
      );
    case 'tool_reference':
      return <span className="conv-chip conv-chip--ref"><ReferenceIcon /> {block.name ?? 'tool'}</span>;
    default:
      return null; // text + tool_call are handled by the walk above
  }
}
