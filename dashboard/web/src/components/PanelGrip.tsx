// Drag handle rendered inside each panel's .panel-header. Persistent on ALL
// pointer types since #249 I1 (low-contrast at rest, brighter on host hover)
// so desktop users can discover drag-reorder without hovering; on touch it
// also provides the `touch-action: none` zone the long-press needs so the
// browser doesn't preempt it as a page-pan. dnd-kit's pointer listeners live
// on the surrounding .panel-host wrapper (PanelHost.tsx); PointerEvents bubble
// up to that listener and start the drag.
export function PanelGrip() {
  return (
    <span className="panel-grip" aria-hidden="true">⋮⋮</span>
  );
}
