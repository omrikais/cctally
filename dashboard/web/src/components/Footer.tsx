import { triggerSync } from '../store/sync';
import { tryQuit } from '../store/actions';

// The `s` and `?` buttons synthesize keydowns so the overlays' already-
// registered keymap actions handle open/close — avoids duplicating the
// state. `q` goes through tryQuit() so non-opener tabs (where
// window.close() is a silent no-op) still surface the "Can't close this
// tab" toast fallback.

export function Footer() {
  return (
    <div className="footer">
      <span className="kb">
        <kbd>↑/↓</kbd> scroll
      </span>
      <span className="kb">
        <kbd className="accent-green">tab</kbd> next panel
      </span>
      <button
        className="kb-btn"
        id="footer-r"
        type="button"
        onClick={() => triggerSync()}
      >
        <kbd className="accent-purple">r</kbd> refresh
      </button>
      <button
        className="kb-btn"
        id="footer-s"
        type="button"
        onClick={() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 's' }))}
      >
        <kbd>s</kbd> settings
      </button>
      <button
        className="kb-btn"
        id="footer-help"
        type="button"
        onClick={() => document.dispatchEvent(new KeyboardEvent('keydown', { key: '?' }))}
      >
        <kbd className="accent-amber">?</kbd> help
      </button>
      <button
        className="kb-btn"
        id="footer-q"
        type="button"
        onClick={() => tryQuit()}
      >
        <kbd>q</kbd> quit
      </button>
    </div>
  );
}
