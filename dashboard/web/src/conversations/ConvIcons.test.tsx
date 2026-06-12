import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReactNode } from 'react';
import {
  toolIcon, ThinkingIcon, FileSearchIcon, TerminalIcon, ToolGenericIcon, LinkIcon,
  SpinnerIcon, QuestionIcon, PlanIcon, GlobeIcon, SearchIcon, PlugIcon,
  BrowserWindowIcon, ChromeIcon, CodexIcon, mcpServerIcon,
} from './ConvIcons';

function svgOf(el: ReactNode) {
  const { container } = render(<>{el}</>);
  return container.querySelector('svg');
}

describe('ConvIcons', () => {
  it('renders an aria-hidden svg with the conv-ico class', () => {
    const svg = svgOf(<ThinkingIcon />);
    expect(svg).toBeInTheDocument();
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).toHaveClass('conv-ico');
  });

  it('toolIcon maps families case-insensitively', () => {
    // same component type for a family member as the exported component
    expect(svgOf(toolIcon('Read'))!.outerHTML).toBe(svgOf(<FileSearchIcon />)!.outerHTML);
    expect(svgOf(toolIcon('grep'))!.outerHTML).toBe(svgOf(<FileSearchIcon />)!.outerHTML);
    expect(svgOf(toolIcon('BASH'))!.outerHTML).toBe(svgOf(<TerminalIcon />)!.outerHTML);
  });

  it('toolIcon falls back to the generic glyph for unknown tools', () => {
    expect(svgOf(toolIcon('Frobnicate'))!.outerHTML).toBe(svgOf(<ToolGenericIcon />)!.outerHTML);
    expect(svgOf(toolIcon(undefined))!.outerHTML).toBe(svgOf(<ToolGenericIcon />)!.outerHTML);
  });

  it('LinkIcon renders an aria-hidden conv-ico svg', () => {
    const svg = svgOf(<LinkIcon />);
    expect(svg).toBeInTheDocument();
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).toHaveClass('conv-ico');
  });

  it('SpinnerIcon renders a conv-ico svg (the animated loading glyph)', () => {
    const svg = svgOf(<SpinnerIcon />);
    expect(svg).toBeInTheDocument();
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).toHaveClass('conv-ico');
  });
});

describe('ConvIcons — Session 2 tool glyphs', () => {
  it('dispatches AskUserQuestion to QuestionIcon and ExitPlanMode to PlanIcon', () => {
    const a = render(<>{toolIcon('AskUserQuestion')}</>).container.querySelector('svg');
    const p = render(<>{toolIcon('ExitPlanMode')}</>).container.querySelector('svg');
    expect(a).toBeTruthy();
    expect(p).toBeTruthy();
  });
  it('QuestionIcon and PlanIcon render an svg', () => {
    expect(render(<QuestionIcon />).container.querySelector('svg')).toBeTruthy();
    expect(render(<PlanIcon />).container.querySelector('svg')).toBeTruthy();
  });
});

describe('ConvIcons — Session 4 MCP + web glyphs', () => {
  it('an MCP name gets its per-server glyph, not the generic box', () => {
    const playwright = svgOf(toolIcon('mcp__plugin_playwright_playwright__browser_click'))!;
    expect(playwright.outerHTML).toBe(svgOf(<BrowserWindowIcon />)!.outerHTML);
    expect(playwright.outerHTML).not.toBe(svgOf(<ToolGenericIcon />)!.outerHTML);
    expect(svgOf(toolIcon('mcp__claude-in-chrome__computer'))!.outerHTML).toBe(svgOf(<ChromeIcon />)!.outerHTML);
    expect(svgOf(toolIcon('mcp__codex__codex-reply'))!.outerHTML).toBe(svgOf(<CodexIcon />)!.outerHTML);
  });

  it('an unknown MCP server falls back to the plug glyph (not the generic box)', () => {
    const unknown = svgOf(toolIcon('mcp__sm-skills__search_skills'))!;
    expect(unknown.outerHTML).toBe(svgOf(<PlugIcon />)!.outerHTML);
    expect(unknown.outerHTML).not.toBe(svgOf(<ToolGenericIcon />)!.outerHTML);
  });

  it('WebSearch maps to the magnifier, WebFetch keeps the globe (#177 S4 split)', () => {
    expect(svgOf(toolIcon('WebSearch'))!.outerHTML).toBe(svgOf(<SearchIcon />)!.outerHTML);
    expect(svgOf(toolIcon('WebSearch'))!.outerHTML).not.toBe(svgOf(<GlobeIcon />)!.outerHTML);
    expect(svgOf(toolIcon('WebFetch'))!.outerHTML).toBe(svgOf(<GlobeIcon />)!.outerHTML);
  });

  it('a non-MCP unknown tool still falls back to the generic box', () => {
    expect(svgOf(toolIcon('Frobnicate'))!.outerHTML).toBe(svgOf(<ToolGenericIcon />)!.outerHTML);
  });

  it('mcpServerIcon dispatches each kind (generic → plug)', () => {
    expect(svgOf(mcpServerIcon('playwright'))!.outerHTML).toBe(svgOf(<BrowserWindowIcon />)!.outerHTML);
    expect(svgOf(mcpServerIcon('generic'))!.outerHTML).toBe(svgOf(<PlugIcon />)!.outerHTML);
  });
});
