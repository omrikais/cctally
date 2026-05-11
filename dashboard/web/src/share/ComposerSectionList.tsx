// Composer left pane — section list with dnd-kit reorder + per-section
// kebab menu (spec §8.3).
//
// dnd-kit invariants (project memory: dndkit-stable-items + dndkit-
// touch-action):
//   - `items` array MUST be stable across renders during a drag. The
//     parent <ComposerModal> sources the array from the basket slice
//     reducer (which returns the SAME identity when no mutation occurs).
//     We only call dispatch(BASKET_REORDER) on `onDragEnd` — never
//     mid-drag — so the dnd-kit sortable context never sees a mutated
//     items array during the pointer-move phase.
//   - The draggable surface (the drag-handle button) has
//     `touch-action: none` in CSS (`.composer-drag-handle` rule) so
//     mobile pointer gestures don't get preempted as page-scroll.
//
// Reorder dispatches `BASKET_REORDER` directly (vs. e.g. recomputing
// the array in local state then mirroring): the master store is the
// source of truth, the recompose pipeline in <ComposerModal> watches
// `basket.items` identity to retrigger /api/share/compose.
import {
  DndContext, closestCenter, KeyboardSensor, PointerSensor,
  useSensor, useSensors, type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext, rectSortingStrategy, sortableKeyboardCoordinates,
  useSortable,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useState } from 'react';
import { dispatch } from '../store/store';
import type { BasketItem } from '../store/basketSlice';
import type { ComposeSectionResult } from './composerApi';

interface Props {
  items: BasketItem[];
  results: ComposeSectionResult[];
  kernelVersion: number;
  onRefresh: (id: string) => void;
  onRemove: (id: string) => void;
  onPreviewOnly: (id: string) => void;
}

export function ComposerSectionList({
  items, results, kernelVersion, onRefresh, onRemove, onPreviewOnly,
}: Props) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  function handleDragEnd(e: DragEndEvent) {
    if (!e.over || e.active.id === e.over.id) return;
    const fromIdx = items.findIndex((it) => it.id === e.active.id);
    const toIdx = items.findIndex((it) => it.id === e.over!.id);
    if (fromIdx < 0 || toIdx < 0) return;
    dispatch({ type: 'BASKET_REORDER', fromIdx, toIdx });
  }

  const ids = items.map((it) => it.id);
  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
      <SortableContext items={ids} strategy={rectSortingStrategy}>
        <ul className="composer-section-list">
          {items.map((item, idx) => (
            <Row
              key={item.id}
              item={item}
              result={results[idx]}
              kernelVersion={kernelVersion}
              onRefresh={onRefresh}
              onRemove={onRemove}
              onPreviewOnly={onPreviewOnly}
            />
          ))}
        </ul>
      </SortableContext>
    </DndContext>
  );
}

function Row({
  item, result, kernelVersion, onRefresh, onRemove, onPreviewOnly,
}: {
  item: BasketItem;
  result: ComposeSectionResult | undefined;
  kernelVersion: number;
  onRefresh: (id: string) => void;
  onRemove: (id: string) => void;
  onPreviewOnly: (id: string) => void;
}) {
  const {
    attributes, listeners, setNodeRef, transform, transition, isDragging,
  } = useSortable({ id: item.id });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };
  const [kebabOpen, setKebabOpen] = useState(false);

  // Outdated badge sources (spec §7.7):
  //   - data drift: the section's `data_digest_at_add` no longer
  //     matches the freshly-computed digest (server signal).
  //   - kernel drift: the section was added under a kernel version
  //     older than the one the server just composed with.
  // We surface a single "Outdated" pill; the tooltip disambiguates so
  // the user knows whether refreshing the section is purely cosmetic
  // (kernel-only) or recovers shifted data.
  const dataDrift = result?.drift_detected;
  const kernelDrift = item.kernel_version !== kernelVersion;
  const outdated = Boolean(dataDrift || kernelDrift);

  return (
    <li ref={setNodeRef} style={style} className="composer-section-row">
      <button
        className="composer-drag-handle"
        aria-label={`Reorder ${item.label_hint}`}
        type="button"
        {...attributes}
        {...listeners}
      >
        ≡
      </button>
      <span className="composer-section-label">{item.label_hint}</span>
      {outdated ? (
        <span
          className="composer-outdated-badge"
          title={
            dataDrift && kernelDrift
              ? 'Data and kernel both shifted since add-time. Refresh to update.'
              : dataDrift
                ? 'Data has changed since this section was added. Refresh to update.'
                : 'Kernel updated since this section was added. Refresh to re-render at the new version.'
          }
        >
          Outdated
        </span>
      ) : null}
      <div className="composer-section-actions">
        <button
          type="button"
          aria-haspopup="menu"
          aria-expanded={kebabOpen}
          onClick={() => setKebabOpen((v) => !v)}
          aria-label={`Actions for ${item.label_hint}`}
        >
          ⋯
        </button>
        {kebabOpen ? (
          <ul role="menu" className="composer-section-menu">
            <li>
              <button
                type="button"
                onClick={() => { onPreviewOnly(item.id); setKebabOpen(false); }}
              >
                Preview only this
              </button>
            </li>
            <li>
              <button
                type="button"
                onClick={() => { onRefresh(item.id); setKebabOpen(false); }}
              >
                Refresh from current data
              </button>
            </li>
            <li>
              <button
                type="button"
                onClick={() => { onRemove(item.id); setKebabOpen(false); }}
                aria-label={`Remove ${item.label_hint}`}
              >
                Remove
              </button>
            </li>
          </ul>
        ) : null}
      </div>
    </li>
  );
}
