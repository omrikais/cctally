import type { ReactNode, SVGProps } from 'react';

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

export function LoadingIcon() {
  return (
    <Svg>
      <path d="M6 4h12M6 20h12M8 4c0 4 8 6 8 8s-8 4-8 8M16 4c0 4-8 6-8 8" />
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

// Per-tool glyph dispatcher: case-insensitive family match, generic box as the
// never-blank fallback. Used by the tool chip + the tool_use degradation chip.
export function toolIcon(name?: string | null): ReactNode {
  const n = (name ?? '').toLowerCase();
  if (n === 'read' || n === 'grep' || n === 'glob' || n === 'ls') return <FileSearchIcon />;
  if (n === 'edit' || n === 'write' || n === 'notebookedit' || n === 'multiedit') return <PencilIcon />;
  if (n === 'bash') return <TerminalIcon />;
  if (n === 'task' || n === 'agent') return <SubagentIcon />;
  if (n === 'webfetch' || n === 'websearch') return <GlobeIcon />;
  if (n === 'todowrite' || n === 'taskcreate') return <ChecklistIcon />;
  return <ToolGenericIcon />;
}
