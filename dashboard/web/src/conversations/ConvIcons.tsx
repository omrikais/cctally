import type { ReactNode, SVGProps } from 'react';
import { parseMcpName, type McpServerKind } from './parseMcpName';

// Inline-SVG icon system for the conversation reader (C3 / H2). Each glyph is a
// 24×24 stroke-only SVG inheriting the chip's per-kind accent through
// `currentColor`, so the existing cyan/indigo/teal accent rules stay in force
// with zero color changes. Every glyph is `aria-hidden="true"`: the adjacent
// text label (Thinking / tool-name / Result / Subagent / …) is the accessible
// meaning — the "not-by-emoji-alone" half of H2. The per-tool `toolIcon(name)`
// dispatcher gives the tool chip a scannable, curated glyph instead of one
// generic mark.

// Shared SVG shell: viewBox, no fill, currentColor stroke, rounded joins, and
// the `.conv-ico` sizing class + aria-hidden on every glyph.
function Svg({ children, ...rest }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      className="conv-ico"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export function ThinkingIcon() {
  return (
    <Svg>
      <path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1h6c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2Z" />
    </Svg>
  );
}

export function ToolGenericIcon() {
  return (
    <Svg>
      <path d="M3 7l9-4 9 4-9 4-9-4Z" />
      <path d="M3 7v10l9 4 9-4V7" />
    </Svg>
  );
}

export function SubagentIcon() {
  return (
    <Svg>
      <circle cx="12" cy="7" r="3" />
      <path d="M5.5 21a6.5 6.5 0 0 1 13 0" />
      <path d="M19 8.5 21 7M19 11l2.4.6" />
    </Svg>
  );
}

export function SystemIcon() {
  return (
    <Svg>
      <path d="M4 6h16M4 12h16M4 18h10" />
    </Svg>
  );
}

// Puzzle piece — the skill glyph. Shared by the Skill tool chip (toolIcon) and
// the standalone "Skill content" meta pill. A paired skill body now folds into
// the Skill tool chip itself (skill-content nesting); the standalone pill — and
// this glyph on it — serves only UNPAIRED skills (SessionStart injection, or the
// pre-reingest window), keeping the two cases visually consistent.
export function SkillIcon() {
  return (
    <Svg>
      <path d="M15.5 3.5a2 2 0 1 0-3.9.5H8a1 1 0 0 0-1 1v3.6a2 2 0 1 1 0 3.8V19a1 1 0 0 0 1 1h3.6a2 2 0 1 0 3.8 0H19a1 1 0 0 0 1-1v-3.6a2 2 0 1 1 0-3.8V5a1 1 0 0 0-1-1h-3.5Z" />
    </Svg>
  );
}

export function FileSearchIcon() {
  return (
    <Svg>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h6" />
      <path d="M14 3v5h5" />
      <circle cx="16.5" cy="16.5" r="3" />
      <path d="M21 21l-2.1-2.1" />
    </Svg>
  );
}

export function PencilIcon() {
  return (
    <Svg>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </Svg>
  );
}

export function TerminalIcon() {
  return (
    <Svg>
      <path d="M4 6l5 6-5 6" />
      <path d="M12 18h8" />
    </Svg>
  );
}

export function GlobeIcon() {
  return (
    <Svg>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3c2.5 2.5 2.5 15.5 0 18M12 3c-2.5 2.5-2.5 15.5 0 18" />
    </Svg>
  );
}

export function ChecklistIcon() {
  return (
    <Svg>
      <path d="M9 6h11M9 12h11M9 18h11" />
      <path d="M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2" />
    </Svg>
  );
}

export function ResultIcon() {
  return (
    <Svg>
      <path d="M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" />
      <path d="M12 3v11M8 10l4 4 4-4" />
    </Svg>
  );
}

export function ReferenceIcon() {
  return (
    <Svg>
      <path d="M9 10l-5 5 5 5" />
      <path d="M4 15h11a5 5 0 0 0 5-5V4" />
    </Svg>
  );
}

export function ImageIcon() {
  return (
    <Svg>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="8.5" cy="8.5" r="1.5" />
      <path d="M21 15l-5-5L5 21" />
    </Svg>
  );
}

export function DocumentIcon() {
  return (
    <Svg>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" />
      <path d="M14 3v5h5" />
    </Svg>
  );
}

// #217 S6 F4 — per-turn bookmark glyphs. The hollow star is the unbookmarked
// state (stroke-only, matching every other glyph); the filled star is the
// bookmarked state (it overrides `fill` to `currentColor` so the accent rule on
// the pressed button paints it solid). The adjacent aria-label on the button
// carries the accessible meaning ("Bookmark this turn" / "Remove bookmark").
export function BookmarkIcon() {
  return (
    <Svg>
      <path d="M12 3l2.6 5.3 5.8.9-4.2 4.1 1 5.8L12 17.8 6.8 19.2l1-5.8L3.6 9.2l5.8-.9Z" />
    </Svg>
  );
}

export function BookmarkFilledIcon() {
  return (
    <Svg fill="currentColor">
      <path d="M12 3l2.6 5.3 5.8.9-4.2 4.1 1 5.8L12 17.8 6.8 19.2l1-5.8L3.6 9.2l5.8-.9Z" />
    </Svg>
  );
}

export function CopyIcon() {
  return (
    <Svg>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h8" />
    </Svg>
  );
}

export function CheckIcon() {
  return (
    <Svg>
      <path d="M5 13l4 4 10-10" />
    </Svg>
  );
}

export function LinkIcon() {
  return (
    <Svg>
      <path d="M9 15l6-6" />
      <path d="M11 6l1-1a4 4 0 0 1 6 6l-1 1" />
      <path d="M13 18l-1 1a4 4 0 0 1-6-6l1-1" />
    </Svg>
  );
}

// Loading spinner: a faint full ring plus a bright quarter-arc. The reader's
// loading-container CSS selector rotates it (the `.conv-ico` shell hardcodes its
// className, so the spin is driven by the container, not a prop); a
// prefers-reduced-motion rule suppresses the rotation.
export function SpinnerIcon() {
  return (
    <Svg>
      <circle cx="12" cy="12" r="9" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" />
    </Svg>
  );
}

export function WarningIcon() {
  return (
    <Svg>
      <path d="M12 3l9 16H3Z" />
      <path d="M12 9v5M12 17v.5" />
    </Svg>
  );
}

export function ChatIcon() {
  return (
    <Svg>
      <path d="M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12Z" />
    </Svg>
  );
}

// Speech bubble with a "?" — the AskUserQuestion glyph. Distinct from ChatIcon
// (the empty-state glyph) so one mark never carries two meanings.
export function QuestionIcon() {
  return (
    <Svg>
      <path d="M21 11.5a8 8 0 0 1-11.6 7.1L3 20.5l1.9-6.4A8 8 0 1 1 21 11.5Z" />
      <path d="M9.6 9a2.4 2.4 0 0 1 4.4 1.3c0 1.6-2 1.9-2 3.2" />
      <path d="M12 16.5v.5" />
    </Svg>
  );
}

// Clipboard with a check — the ExitPlanMode (plan) glyph.
export function PlanIcon() {
  return (
    <Svg>
      <path d="M9 4h6a1 1 0 0 1 1 1v1H8V5a1 1 0 0 1 1-1Z" />
      <path d="M8 6H6a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1h-2" />
      <path d="M9 13l2 2 4-4" />
    </Svg>
  );
}

// Browser window — the playwright server glyph (#177 S4).
export function BrowserWindowIcon() {
  return (
    <Svg>
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="M2 9h20M5.5 6.5h.01M8.5 6.5h.01" />
    </Svg>
  );
}

// Chrome rings — the claude-in-chrome server glyph (#177 S4).
export function ChromeIcon() {
  return (
    <Svg>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="3.5" />
      <path d="M12 8.5 19.5 7M8.9 13.8 4.6 19M15.1 13.8l4.3 5.2" />
    </Svg>
  );
}

// Monitor — the computer-use server glyph (#177 S4).
export function MonitorIcon() {
  return (
    <Svg>
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M9 20h6M12 16v4" />
    </Svg>
  );
}

// Prompt chevron — the codex server glyph (#177 S4).
export function CodexIcon() {
  return (
    <Svg>
      <path d="M4 17l6-5-6-5M12 19h8" />
    </Svg>
  );
}

// Plug — the generic MCP-server fallback glyph (#177 S4).
export function PlugIcon() {
  return (
    <Svg>
      <path d="M9 7V3M15 7V3" />
      <path d="M6 7h12v4a6 6 0 0 1-12 0V7Z" />
      <path d="M12 17v4" />
    </Svg>
  );
}

// Magnifier — WebSearch (GlobeIcon stays on WebFetch; #177 S4 spec §4.4).
export function SearchIcon() {
  return (
    <Svg>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </Svg>
  );
}

// Per-MCP-server glyph (#177 S4).
export function mcpServerIcon(kind: McpServerKind): ReactNode {
  switch (kind) {
    case 'playwright': return <BrowserWindowIcon />;
    case 'chrome': return <ChromeIcon />;
    case 'computer': return <MonitorIcon />;
    case 'codex': return <CodexIcon />;
    default: return <PlugIcon />;
  }
}

// Per-tool glyph dispatcher: case-insensitive family match, generic box as the
// never-blank fallback. Used by the tool chip + the tool_use degradation chip.
// #177 S4: the MCP branch runs FIRST — an mcp__ name gets its per-server glyph;
// WebSearch splits off the globe onto the magnifier (the globe stays on
// WebFetch); non-MCP unknowns keep the generic box.
export function toolIcon(name?: string | null): ReactNode {
  const mcp = parseMcpName(name);       // #177 S4: per-server glyph
  if (mcp) return mcpServerIcon(mcp.kind);
  const n = (name ?? '').toLowerCase();
  if (n === 'read' || n === 'grep' || n === 'glob' || n === 'ls') return <FileSearchIcon />;
  if (n === 'edit' || n === 'write' || n === 'notebookedit' || n === 'multiedit'
      || n === 'apply_patch' || n === 'patch_apply_end') return <PencilIcon />;
  if (n === 'bash' || n === 'exec') return <TerminalIcon />;
  if (n === 'task' || n === 'agent') return <SubagentIcon />;
  if (n === 'skill') return <SkillIcon />;
  if (n === 'webfetch') return <GlobeIcon />;
  if (n === 'websearch') return <SearchIcon />;
  if (n === 'askuserquestion') return <QuestionIcon />;
  if (n === 'exitplanmode') return <PlanIcon />;
  if (n === 'todowrite' || n === 'taskcreate') return <ChecklistIcon />;
  return <ToolGenericIcon />;
}
