// TemplateGallery — plan §M1.12 contract:
//   - Renders 3 archetype tiles (Recap / Visual / Detail).
//   - In M1 only Recap is populated by the server; Visual/Detail
//     render as disabled placeholders.
//   - Error banner renders when `error` prop is set.
//   - Clicking a populated tile fires onSelect with the template id.
//   - Loading state (templates=null) renders skeleton tiles.
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TemplateGallery } from './TemplateGallery';
import type { ShareTemplate } from './types';

const recap: ShareTemplate = {
  id: 'weekly-recap',
  label: 'Recap',
  description: 'Text + tiny chart',
  default_options: {},
};

describe('<TemplateGallery>', () => {
  it('renders skeleton tiles while loading', () => {
    const { container } = render(
      <TemplateGallery
        panel="weekly"
        templates={null}
        error={null}
        selectedTemplateId={null}
        onSelect={() => {}}
      />,
    );
    expect(container.querySelectorAll('.share-tile-skeleton')).toHaveLength(3);
  });

  it('renders error banner when fetch failed', () => {
    render(
      <TemplateGallery
        panel="weekly"
        templates={null}
        error="Couldn't load templates: HTTP 500"
        selectedTemplateId={null}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(/couldn't load templates/i);
  });

  it('renders Recap as selectable and Visual/Detail as disabled placeholders', () => {
    render(
      <TemplateGallery
        panel="weekly"
        templates={[recap]}
        error={null}
        selectedTemplateId="weekly-recap"
        onSelect={() => {}}
      />,
    );
    // Recap tile is enabled and marked selected.
    const recapBtn = screen.getByRole('radio', { name: /recap/i });
    expect(recapBtn).not.toBeDisabled();
    expect(recapBtn).toHaveAttribute('aria-checked', 'true');

    // Visual and Detail are present but disabled.
    const visual = screen.getByRole('radio', { name: /visual/i });
    expect(visual).toBeDisabled();
    expect(visual).toHaveAttribute('aria-checked', 'false');
    const detail = screen.getByRole('radio', { name: /detail/i });
    expect(detail).toBeDisabled();
  });

  it('fires onSelect when a populated tile is clicked', () => {
    const onSelect = vi.fn();
    render(
      <TemplateGallery
        panel="weekly"
        templates={[recap]}
        error={null}
        selectedTemplateId={null}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByRole('radio', { name: /recap/i }));
    expect(onSelect).toHaveBeenCalledWith('weekly-recap');
  });
});
