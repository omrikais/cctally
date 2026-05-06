// Touch-only drag handle rendered inside each panel's .panel-header.
// Hidden on devices with a fine pointer (mouse/trackpad) via CSS @media,
// so desktop users see no chrome change. dnd-kit's pointer listeners
// live on the surrounding .panel-host wrapper (PanelHost.tsx); the grip's
// only job is to provide a `touch-action: none` zone where the browser
// won't preempt the long-press as a page-pan. PointerEvents bubble up to
// the panel-host listener and start the drag.
export function PanelGrip() {
  return (
    <span className="panel-grip" aria-hidden="true">⋮⋮</span>
  );
}
