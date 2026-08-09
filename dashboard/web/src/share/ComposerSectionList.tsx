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
import { useMemo, useState } from 'react';
import { dispatch } from '../store/store';
import { useKeymap } from '../hooks/useKeymap';
import { useReducedMotion } from '../hooks/useReducedMotion';
import type { BasketItem } from '../store/basketSlice';
import type { ComposeSectionResult } from './composerApi';
import { SELECTION_LABEL } from './types';

interface Props {
  items: BasketItem[];
  results: ComposeSectionResult[];
  kernelVersion: number;
  onRefresh: (id: string) => void;
  onRemove: (id: string) => void;
}

export function ComposerSectionList({
  items, results, kernelVersion, onRefresh, onRemove,
}: Props) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  // #531.1 — each row held its own `kebabOpen` with nothing coordinating them,
  // so several menus could be open at once and Escape had no owner: it fell
  // through to the composer's layer-210 binding and closed the whole composer,
  // taking unsaved title/theme/format edits with it. The open id lives here so
  // opening one disclosure closes any other.
  const [openKebabId, setOpenKebabId] = useState<string | null>(null);

  // Layer 215: above the composer's own 210 so this fires first, below an
  // armed confirmation at 220 which must keep owning Esc. ONE binding at the
  // parent, not one per row — twenty bindings at the same layer resolve by
  // insertion order, which is nondeterministic exactly when two are open.
  //
  // A local `stopPropagation` handler (as SavePresetPopover does) would bypass
  // the central ordering; that popover is a deliberately reconciled special
  // case rather than the house pattern.
  const bindings = useMemo(
    () => [{
      key: 'Escape',
      scope: 'overlay' as const,
      layer: 215,
      when: () => openKebabId !== null,
      action: () => {
        const id = openKebabId;
        setOpenKebabId(null);
        if (id) document.getElementById(`composer-kebab-${id}`)?.focus();
      },
    }],
    [openKebabId],
  );
  useKeymap(bindings);

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
              isKebabOpen={openKebabId === item.id}
              onKebabToggle={() =>
                setOpenKebabId((p) => (p === item.id ? null : item.id))}
              onKebabClose={() => setOpenKebabId(null)}
            />
          ))}
        </ul>
      </SortableContext>
    </DndContext>
  );
}

function Row({
  item, result, kernelVersion, onRefresh, onRemove,
  isKebabOpen, onKebabToggle, onKebabClose,
}: {
  item: BasketItem;
  result: ComposeSectionResult | undefined;
  kernelVersion: number;
  onRefresh: (id: string) => void;
  onRemove: (id: string) => void;
  // Lifted to <ComposerSectionList> so at most one disclosure is open and one
  // Escape binding owns them all (#531.1).
  isKebabOpen: boolean;
  onKebabToggle: () => void;
  onKebabClose: () => void;
}) {
  const {
    attributes, listeners, setNodeRef, transform, transition, isDragging,
  } = useSortable({ id: item.id });
  // dnd-kit returns an inline `transition` value for the drag-overlay
  // smoothing — the rule sits in inline style, so CSS @media reduced-
  // motion can't override it without `!important` (which the library
  // would clobber on next render). Gate JS-side, mirroring
  // <PanelHost>'s pattern. Spec §12.6 + M4.4.
  const reducedMotion = useReducedMotion();
  const style = {
    transform: CSS.Transform.toString(transform),
    transition: reducedMotion ? undefined : transition,
    opacity: isDragging ? 0.4 : 1,
  };
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
      {/* #294 S5 §7 — always-visible per-item source chip, so a mixed-source
          basket reads apart at a glance. */}
      <span className={`source-chip source-chip--${item.source}`}>
        {SELECTION_LABEL[item.source]}
      </span>
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
          id={`composer-kebab-${item.id}`}
          aria-expanded={isKebabOpen}
          /* Only while the list exists. The <ul> renders conditionally, so an
             unconditional `aria-controls` points at a missing id whenever the
             disclosure is closed — which is `aria-valid-attr-value`, an ARIA
             violation in the very attribute added to make this control honest.
             Nothing in this repo runs axe, so it would have failed silently. */
          aria-controls={isKebabOpen ? `composer-kebab-menu-${item.id}` : undefined}
          onClick={onKebabToggle}
          aria-label={`Actions for ${item.label_hint}`}
        >
          ⋯
        </button>
        {isKebabOpen ? (
          /* #531.3 — this was `role="menu"` whose children were neither
             `menuitem` nor arrow-navigable. With exactly two actions, both
             already Tab-reachable, completing the ARIA menu contract would
             mean roving tabindex, Arrow keys, Home/End and typeahead — new
             interaction behavior in a corrections-only session. Dropping the
             claim restores truth at no cost to what works. The repository's
             reusable roving implementation (conversations/menuKeyboard.ts)
             stays reserved for surfaces that need menu semantics. */
          <ul
            id={`composer-kebab-menu-${item.id}`}
            className="composer-section-menu"
          >
            {/* #503 S3 §6 — "Preview only this" is gone. It was a menu item
                wired to an empty function: clicking it did nothing, with no
                feedback. That is a broken control, and removing it is the
                correction — the user loses nothing. (The Projects-allowlist
                placeholder stays: it is disabled and already states
                "Exports include all projects", so it makes no false
                promise.) */}
            <li>
              <button
                type="button"
                onClick={() => { onRefresh(item.id); onKebabClose(); }}
              >
                Refresh from current data
              </button>
            </li>
            <li>
              <button
                type="button"
                onClick={() => { onRemove(item.id); onKebabClose(); }}
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
