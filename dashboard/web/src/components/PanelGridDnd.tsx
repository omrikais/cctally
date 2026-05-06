import { useMemo, type ReactNode } from 'react';
import {
  DndContext,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  rectSortingStrategy,
} from '@dnd-kit/sortable';
import { dispatch, getState } from '../store/store';
import { armClickSuppression } from '../lib/clickSuppression';
import type { PanelId } from '../lib/panelRegistry';

const ACTIVATION_DELAY_MS = 250;
const ACTIVATION_TOLERANCE_PX = 5;

// Pure store-wiring functions, exported for direct testing without spinning up
// dnd-kit's pointer mechanics (which require non-zero DOM rects to compute
// collision detection — not available in jsdom).

export function handleDragStartAction(): void {
  // Arm click suppression as soon as the drag activates so the synthesized
  // click that fires after pointerup is swallowed by the panel's
  // onClickCapture handler.
  armClickSuppression();
}

export function handleDragEndAction(
  activeId: PanelId,
  overId: PanelId | null,
): void {
  armClickSuppression();
  if (!overId || activeId === overId) {
    // Released outside any droppable, or same panel → nothing to commit.
    return;
  }
  const order = getState().prefs.panelOrder;
  const from = order.indexOf(activeId);
  const to = order.indexOf(overId);
  if (from < 0 || to < 0 || from === to) return;
  dispatch({ type: 'REORDER_PANELS', from, to });
}

export function handleDragCancelAction(): void {
  armClickSuppression();
}

export function PanelGridDnd({
  items,
  children,
}: {
  items: PanelId[];
  children: ReactNode;
}) {
  // delay/tolerance combine to give a long-press feel: a quick click never
  // activates a drag (preserving the panel's own onClick), and small wobbles
  // during the press are tolerated. dnd-kit's PointerSensor handles
  // pointer-capture, hit-testing, and synthesized-click suppression
  // internally — no Chrome-specific workarounds needed.
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        delay: ACTIVATION_DELAY_MS,
        tolerance: ACTIVATION_TOLERANCE_PX,
      },
    }),
  );

  // The items array MUST stay stable during a drag — rectSortingStrategy
  // reorders items visually via per-item CSS transforms, and the array stays
  // the source of truth for stable layout. Mutating items inside onDragOver
  // creates a feedback loop: new items → strategy recomputes → over-target
  // changes → onDragOver fires again → infinite render loop. We commit the
  // real reorder once on onDragEnd instead.
  const ids = useMemo(() => items.slice(), [items]);

  return (
    <DndContext
      sensors={sensors}
      onDragStart={() => handleDragStartAction()}
      onDragEnd={(e: DragEndEvent) => {
        const activeId = e.active.id as PanelId;
        const overId = e.over ? (e.over.id as PanelId) : null;
        handleDragEndAction(activeId, overId);
      }}
      onDragCancel={() => handleDragCancelAction()}
    >
      <SortableContext items={ids} strategy={rectSortingStrategy}>
        {children}
      </SortableContext>
    </DndContext>
  );
}
