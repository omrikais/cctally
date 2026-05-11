// 3-tile template gallery row (spec §6.2 anatomy, plan §M1.12).
//
// In M1, the server only returns the single "Recap" template per panel
// (one of 8 entries in SHARE_TEMPLATES). The other two archetypes
// (Visual, Detail) ship in M2. To keep the layout stable across M1→M2
// the gallery always renders three tile slots: any archetype that is
// not present in the server's `templates` list renders as a disabled
// "Coming soon" placeholder.
//
// Tile state per spec §6.2:
//   - Selected: highlighted border + checkmark.
//   - Selectable: focusable button with onSelect callback.
//   - Placeholder: disabled, aria-disabled="true", "Coming soon" copy.
//
// Loading / error are handled by the parent (ShareModal): the parent
// fetches templates, then passes either {templates: [...]}, or
// {templates: null} (loading), or {error: "..."} (failed). Keeping
// fetch out of this component means the parent owns the fetch lifecycle
// (cancellation across re-renders, retry on panel change, etc.).
import type { SharePanelId, ShareTemplate } from './types';

// The fixed display order matches spec §6.2 Recap → Visual → Detail.
// Each entry contains its placeholder label/description shown when the
// server has not yet returned a template for that archetype.
const ARCHETYPES: ReadonlyArray<{
  id: 'recap' | 'visual' | 'detail';
  label: string;
  placeholderDescription: string;
}> = [
  { id: 'recap', label: 'Recap', placeholderDescription: 'Text + tiny chart' },
  { id: 'visual', label: 'Visual', placeholderDescription: 'Coming in M2' },
  { id: 'detail', label: 'Detail', placeholderDescription: 'Coming in M2' },
];

// Map a server-returned template.id to its archetype. The kernel's
// `SHARE_TEMPLATES` registry IDs look like `weekly-recap`,
// `weekly-visual`, etc. — the suffix is the archetype.
function archetypeOf(templateId: string): 'recap' | 'visual' | 'detail' | null {
  if (templateId.endsWith('-recap')) return 'recap';
  if (templateId.endsWith('-visual')) return 'visual';
  if (templateId.endsWith('-detail')) return 'detail';
  return null;
}

interface Props {
  panel: SharePanelId;
  // `null` = loading (skeletons render). `[]` = empty (shouldn't happen
  // in M1 — the registry guarantees a Recap per panel — but defensive).
  templates: ShareTemplate[] | null;
  error: string | null;
  selectedTemplateId: string | null;
  onSelect: (templateId: string) => void;
}

export function TemplateGallery({
  panel: _panel,
  templates,
  error,
  selectedTemplateId,
  onSelect,
}: Props) {
  // `panel` is reserved for future archetype-set lookup (per-panel
  // overrides). Suffixed with `_panel` to silence the unused-prop lint.
  void _panel;

  if (error) {
    return (
      <div className="share-gallery-error" role="alert">
        {error}
      </div>
    );
  }

  // Build a quick archetype → template lookup so the fixed display
  // order can be honored even if the server returns templates in a
  // different sequence.
  const byArchetype: Partial<Record<'recap' | 'visual' | 'detail', ShareTemplate>> = {};
  for (const t of templates ?? []) {
    const arch = archetypeOf(t.id);
    if (arch) byArchetype[arch] = t;
  }

  return (
    <div className="share-gallery" role="radiogroup" aria-label="Report template">
      {ARCHETYPES.map((archetype) => {
        const tmpl = byArchetype[archetype.id];
        if (templates == null) {
          // Skeleton — server still loading. One per archetype slot.
          return (
            <div
              key={archetype.id}
              className="share-tile share-tile-skeleton"
              aria-hidden="true"
            >
              <div className="share-tile-skeleton-bar" />
              <div className="share-tile-skeleton-bar short" />
            </div>
          );
        }
        if (!tmpl) {
          // Archetype not yet shipped by the server (M2+). Greyed out.
          return (
            <button
              key={archetype.id}
              type="button"
              className="share-tile share-tile-disabled"
              disabled
              aria-disabled="true"
              role="radio"
              aria-checked="false"
              title={`${archetype.label} — ${archetype.placeholderDescription}`}
            >
              <div className="share-tile-label">{archetype.label}</div>
              <div className="share-tile-desc">{archetype.placeholderDescription}</div>
            </button>
          );
        }
        const isSelected = tmpl.id === selectedTemplateId;
        return (
          <button
            key={archetype.id}
            type="button"
            className={'share-tile' + (isSelected ? ' share-tile-selected' : '')}
            role="radio"
            aria-checked={isSelected}
            data-template-id={tmpl.id}
            onClick={() => onSelect(tmpl.id)}
          >
            <div className="share-tile-label">{tmpl.label}</div>
            <div className="share-tile-desc">{tmpl.description}</div>
          </button>
        );
      })}
    </div>
  );
}
