// #513 S2 §2.1 — the section index rail.
//
// One scroller, one Save, one focus trap: the rail is navigation over the same
// list, not a tab set that would split the form. Selecting an entry scrolls its
// section into view and moves focus to its heading.
//
// The active entry is NOT read off callback ordering. An `IntersectionObserver`
// at threshold 0 tells you THAT something crossed, not WHICH section a reader
// is looking at, and the callback order for two simultaneous crossings is not
// something to build a rule on. The observer is only a trigger here; the rule
// is measured: the active section is the LAST section whose heading has crossed
// the anchor, ties broken by DOM order, clamped to the first section while
// everything is still below the anchor and to the last section at maximum
// scroll.
import { useCallback, useEffect, useRef, useState } from 'react';
import type { SectionId } from './registry';

export interface RailSection {
  id: SectionId;
  title: string;
  dirty: boolean;
}

export interface SettingsRailProps {
  sections: readonly RailSection[];
  activeId: SectionId | null;
  onJump(id: SectionId): void;
}

export function SettingsRail({ sections, activeId, onJump }: SettingsRailProps) {
  return (
    <nav className="settings-rail" aria-label="Settings sections">
      <ul>
        {sections.map((section) => (
          <li key={section.id}>
            <button
              type="button"
              className={`settings-rail-link${section.id === activeId ? ' is-active' : ''}`}
              aria-current={section.id === activeId ? 'location' : undefined}
              onClick={() => onJump(section.id)}
              // Below 768px the rail is a horizontally scrolling strip, and a
              // focused entry past its right edge stayed clipped with
              // `scrollLeft` at 0 — measured at 390px, "Conversation viewer"
              // 23.6% visible with 120.2px cut off, unchanged after 700ms with
              // `scroll-behavior: auto`, so not a timing artifact. This is the
              // same defect §4.4 removed on the vertical axis; the browser
              // performs no corrective scroll here either, so the entry is
              // brought into view explicitly. `nearest` on both axes moves the
              // minimum distance and leaves an already-visible entry alone.
              onFocus={(event) =>
                event.currentTarget.scrollIntoView?.({
                  block: 'nearest',
                  inline: 'nearest',
                })
              }
            >
              {section.title}
              {section.dirty && (
                <>
                  <span className="fs-changed" aria-hidden="true"> ●</span>
                  {/* §4.3 — the dot alone is not an announcement. */}
                  <span className="sr-only"> (has unsaved changes)</span>
                </>
              )}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

// Measured active-section tracking. `scroller` is the element that carries the
// vertical overflow; `anchorOffset` is how far below its top edge a heading
// counts as "crossed", and it is recomputed whenever the action bar's measured
// height changes, because that is what decides how much of the scrollport a
// reader can actually see.
export function useActiveSection(
  scrollerRef: React.RefObject<HTMLElement | null>,
  sectionIds: readonly SectionId[],
  anchorOffset: number,
): SectionId | null {
  const [active, setActive] = useState<SectionId | null>(sectionIds[0] ?? null);
  const idsKey = sectionIds.join('|');
  const lastIds = useRef(sectionIds);
  lastIds.current = sectionIds;

  const recompute = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const ids = lastIds.current;
    if (ids.length === 0) {
      setActive(null);
      return;
    }
    const anchor = scroller.getBoundingClientRect().top + anchorOffset;
    // At maximum scroll the last section may still sit below the anchor while
    // it is plainly what the reader is looking at, so clamp there.
    const atEnd =
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 1;
    if (atEnd) {
      setActive(ids[ids.length - 1]);
      return;
    }
    let candidate: SectionId = ids[0];
    for (const id of ids) {
      const heading = scroller.querySelector<HTMLElement>(
        `[data-settings-section="${id}"]`,
      );
      if (!heading) continue;
      if (heading.getBoundingClientRect().top <= anchor) candidate = id;
    }
    setActive(candidate);
  }, [scrollerRef, anchorOffset]);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    recompute();
    scroller.addEventListener('scroll', recompute, { passive: true });
    // The observer exists to catch layout changes that move a heading without
    // a scroll event — a filter removing rows, a section collapsing.
    let observer: IntersectionObserver | null = null;
    if (typeof IntersectionObserver !== 'undefined') {
      observer = new IntersectionObserver(() => recompute(), {
        root: scroller,
        rootMargin: `-${anchorOffset}px 0px 0px 0px`,
        threshold: 0,
      });
      for (const id of lastIds.current) {
        const heading = scroller.querySelector(`[data-settings-section="${id}"]`);
        if (heading) observer.observe(heading);
      }
    }
    return () => {
      scroller.removeEventListener('scroll', recompute);
      observer?.disconnect();
    };
    // Recreated when the section set or the measured anchor changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recompute, idsKey, anchorOffset]);

  return active;
}
