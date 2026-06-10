import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReactNode } from 'react';
import { toolIcon, ThinkingIcon, FileSearchIcon, TerminalIcon, ToolGenericIcon, LinkIcon } from './ConvIcons';

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
});
