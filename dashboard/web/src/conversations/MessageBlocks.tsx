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
import { useFocusMode, useSuppressedHeadingKeys } from './TranscriptContext';
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
// #463 S2 §2.6 — a reasoning block that will render NOTHING: every heading it
// carries is a repeat an earlier block of the same turn already rendered, and it
// retains no body to disclose. The walk below has to make this call itself
// rather than leave it to the block component, because `out.length` counts React
// ELEMENTS and an element that renders null still produces the `.conv-blocks`
// container (which carries a margin) and the MessageItem header row around it —
// an assistant card with a model chip and no content.
export function reasoningBlockIsEmpty(
  block: ConversationBlock, suppressed: ReadonlySet<string>,
): boolean {
  if (block.kind !== 'codex_reasoning') return false;
  if (!block.headings?.length || block.body != null) return false;
  return block.headings.every((heading) => suppressed.has(heading.key));
}

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
  const suppressedHeadings = useSuppressedHeadingKeys();
  const out: ReactNode[] = [];
  let i = 0;
  while (i < blocks.length) {
    const b = blocks[i];
    // §2.6 — drop a block that renders nothing HERE, so the container and the
    // turn's header row go with it.
    if (reasoningBlockIsEmpty(b, suppressedHeadings)) {
      i++;
      continue;
    }
    if (b.kind === 'text') {
      // #463 S2 §3.2 — one container per SOURCE text block. Before this, a
      // maximal run of consecutive text blocks was joined with "\n\n" into one
      // <Markdown>, so separately authored messages read as a single message.
      // Measured on a real store: 939 of 13,072 served segments hold such a run,
      // the longest 40 messages deep.
      //
      // The treatment is separation and nothing else — no per-message timestamp,
      // model chip, cost or header. The timestamps exist on the wire, but the
      // measured elapsed time inside a welded run is a median of 0.002s, so
      // rendering them would present noise as signal. Cost stays on the turn.
      //
      // §3.3, measured with the real renderer rather than a heuristic: joined
      // and split rendering differ on exactly 4 of those 939 runs, and on every
      // one of them a non-last block ends with an unterminated ``` fence. Joined,
      // that fence swallowed every later message into a code block — up to 22
      // whole messages. Splitting is therefore the corrected rendering on all
      // four, which is why the split is unconditional.
      out.push(
        <div className="conv-block" key={`t${out.length}`}
             {...(b.block_key ? { 'data-block-key': b.block_key } : {})}>
          <Markdown>{b.text}</Markdown>
        </div>,
      );
      i++;
      continue;
    }
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
  // Backgrounded MCP call (spec 2026-07-31 §5). RECOVERED is
  // `background_completed_at` being set — never `background_status ===
  // 'completed'`, which an unrecovered call can also claim while `result` is
  // still the "still running after 120s" placeholder (see the block type).
  // Without this the chip surfaced only error/truncated, so a call that never
  // came back looked exactly like one that did.
  const bgDone = call.background_completed_at ?? null;
  const bgPending = !!call.background_status && bgDone == null;
  // The background label is a RIGID summary child; at 166px it squeezed
  // .conv-chip-preview to 6px on a 390px viewport. Each label therefore has a
  // short wording too, and the @media rule picks one (.conv-status-wide/
  // -narrow in index.css). ' · truncated' rides on EVERY non-error branch —
  // pending included, since a clipped placeholder is still clipped.
  const bgWide = bgPending ? ' · ⋯ running in background' : bgDone ? ' · ran in background' : '';
  const bgNarrow = bgPending ? ' · ⋯ bg' : bgDone ? ' · bg' : '';
  const truncated = call.result?.truncated ? ' · truncated' : '';
  const isErr = !!call.result?.is_error;
  const status = isErr ? ' · error' : bgWide + truncated;
  const statusShort = !isErr && bgNarrow ? bgNarrow + truncated : null;
  return (
    <details className="conv-chip conv-chip--tool">
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        {toolIcon(call.name)} <ChipName name={call.name} />
        <span className="conv-chip-preview">{call.preview}</span>
        {status && (
          <span className="conv-chip-status" title={statusShort ? status.trim() : undefined}>
            {statusShort ? (
              <>
                <span className="conv-status-wide">{status}</span>
                <span className="conv-status-narrow">{statusShort}</span>
              </>
            ) : status}
          </span>
        )}
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
              result{call.result.is_error
                ? ' · error'
                : bgPending ? ' · running in background' : ' · ok'}
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
  // #463 S2 §2.6 — body is null for every reasoning block in the measured
  // corpus (0 of 10,471 post-dedup), so `expandable` is always false TODAY and
  // every block renders as a plain line. The wire can still carry a body, so
  // this is a fact about current data and NOT an unreachable code path: do not
  // delete the disclosure branch below and do not "fix" the always-false gate.
  const expandable = block.body != null;
  // §2.6 — prefer the decomposed headings when the server supplied them, so each
  // authored heading is its own readable, individually addressable line. Falls
  // back to the single headline when absent, which is what a pre-#463-S2 server
  // and an unreadable retained payload both produce.
  //
  // A heading an earlier block of the same TURN already rendered is dropped:
  // Codex writes cumulative summaries, so consecutive blocks re-state each
  // other's headings, which cost one clipped line before S2 and cost a full line
  // each after it. The reader computes the set (it needs the whole turn, which
  // one block cannot see); see suppressRepeatedHeadings.ts for the measurement.
  const suppressed = useSuppressedHeadingKeys();
  const kept = block.headings?.filter((heading) => !suppressed.has(heading.key));
  const headings = kept?.length ? kept : null;
  // Every heading this block decomposed into was a repeat. With no body to
  // disclose the block would render as an empty REASONING label, so it is
  // dropped (MessageBlocks drops it a step earlier so the container goes too;
  // this guard keeps the component correct on its own). A retained body still
  // earns its disclosure — but NOT the headline fallback: for a cumulative
  // aggregate the stored projection IS the summary blob, so falling through to
  // `title ?? summary ?? body` printed every heading the rule had just
  // suppressed, undecomposed and unclipped.
  const allHeadingsSuppressed = Boolean(block.headings?.length) && !headings;
  if (allHeadingsSuppressed && block.body == null) return null;
  const summary = (
    <>
      {expandable && <span className="conv-chev" aria-hidden="true" />}
      <ThinkingIcon />
      <span className="conv-codex-reasoning-label">Reasoning</span>
      {headings ? (
        <span className="conv-codex-reasoning-headings">
          {headings.map((heading) => (
            <span className="conv-codex-reasoning-title" key={heading.key}
                  data-heading-key={heading.key}>
              {heading.text}
            </span>
          ))}
        </span>
      ) : allHeadingsSuppressed ? null : (
        <span className="conv-codex-reasoning-title"><Markdown>{headline}</Markdown></span>
      )}
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
