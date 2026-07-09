import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useConversationFacets } from '../hooks/useConversationFacets';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { modelChipClass } from '../lib/model';
import type { ConversationFilters } from '../types/conversation';

// Browse-list filters popover (filters spec §4). Holds all four filter axes —
// date (preset chips + from/to range), project (multi-select from the facets
// endpoint), cost (min/max + quick presets), and cache-rebuilds (threshold
// presets + custom ≥N). Filters apply LIVE (numeric/text inputs debounced
// ~300ms); the footer "Done" only closes the popover. The numeric/text inputs
// dispatch SET_INPUT_MODE on focus/blur so single-char reader hotkeys are
// suppressed while typing (Task 4's named-key End binding additionally gates on
// convFiltersOpen). Renders only while convFiltersOpen (the rail owns that gate).

// Format a Date's UTC Y/M/D as 'YYYY-MM-DD'. Inputs are built from Date.UTC of a
// wall-clock Y/M/D, so reading the UTC fields back out is a pure format step.
function ymdUtc(d: Date): string {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, '0');
  const day = String(d.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

// "Today" as wall-clock Y/M/D in the display tz (matching railDateBucket's
// ymdInTz). The server resolves day boundaries in display.tz, so the preset
// month must be derived from the display-tz calendar day — NOT raw UTC, which
// for a user far behind UTC near a month boundary would land on the wrong month.
// `tz` is the snapshot's resolved IANA zone (Etc/UTC fallback), so Intl handles
// DST correctly.
function todayInTz(tz: string): { y: number; m: number; d: number } {
  const [y, m, d] = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date()).split('-').map(Number);
  return { y, m, d };
}

interface DatePreset { key: string; label: string; bounds: (tz: string) => { from: string; to: string } }

// Preset boundary calculators. "Now" is the display-tz calendar day (todayInTz);
// the Y/M/D is then projected through Date.UTC so the YYYY-MM-DD math
// (month-roll, last-day-of-month, 7-day window) is host-TZ-independent.
// `this-month`/`last-month` use the Date.UTC(y, m+1, 0) "last day of month"
// idiom; `last-7d` is a 7-day window ending today (inclusive).
const DATE_PRESETS: DatePreset[] = [
  {
    key: 'this-month', label: 'This month',
    bounds: (tz) => {
      const { y, m } = todayInTz(tz);
      return { from: ymdUtc(new Date(Date.UTC(y, m - 1, 1))), to: ymdUtc(new Date(Date.UTC(y, m, 0))) };
    },
  },
  {
    key: 'last-month', label: 'Last month',
    bounds: (tz) => {
      const { y, m } = todayInTz(tz);
      return { from: ymdUtc(new Date(Date.UTC(y, m - 2, 1))), to: ymdUtc(new Date(Date.UTC(y, m - 1, 0))) };
    },
  },
  {
    key: 'last-7d', label: 'Last 7d',
    bounds: (tz) => {
      const { y, m, d } = todayInTz(tz);
      const today = new Date(Date.UTC(y, m - 1, d));
      const from = new Date(today.getTime() - 6 * 86_400_000);
      return { from: ymdUtc(from), to: ymdUtc(today) };
    },
  },
];

const COST_PRESETS = [1, 5, 10];
const REBUILD_PRESETS = [1, 3, 5];

// Set one filter axis. A thin wrapper so every control reads as a one-axis patch.
function patch(p: Partial<ConversationFilters>): void {
  dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: p });
}

export function ConversationFiltersPopover() {
  const filters = useSyncExternalStore(subscribeStore, () => getState().conversationFilters);
  const facets = useConversationFacets();
  // Resolved display tz (concrete IANA zone, Etc/UTC fallback) drives which
  // calendar month a date preset lands on, so it matches the server's display-tz
  // interpretation of the YYYY-MM-DD bounds (FINDING 4).
  const { resolvedTz } = useDisplayTz();

  // Local mirror of the debounced numeric/text inputs so typing feels instant
  // while the store update (and therefore the refetch) is debounced ~300ms. The
  // displayed values follow the store on an external change (e.g. Clear all).
  const [costMinStr, setCostMinStr] = useState(filters.costMin?.toString() ?? '');
  const [costMaxStr, setCostMaxStr] = useState(filters.costMax?.toString() ?? '');
  const [rebuildStr, setRebuildStr] = useState(filters.rebuildMin?.toString() ?? '');
  useEffect(() => { setCostMinStr(filters.costMin?.toString() ?? ''); }, [filters.costMin]);
  useEffect(() => { setCostMaxStr(filters.costMax?.toString() ?? ''); }, [filters.costMax]);
  useEffect(() => { setRebuildStr(filters.rebuildMin?.toString() ?? ''); }, [filters.rebuildMin]);

  // Per-field debounce timers, keyed by axis ('costMin'|'costMax'|'rebuildMin').
  // INDEPENDENT timers — a SINGLE shared timer would let editing a second numeric
  // within the 300ms window cancel the first field's pending dispatch (the first
  // input would still DISPLAY its value while the store never receives it). All
  // pending timers are cleared on unmount.
  const debounceRefs = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  useEffect(() => {
    const timers = debounceRefs.current;
    return () => { for (const k in timers) clearTimeout(timers[k]); };
  }, []);
  const debounced = (key: string, fn: () => void): void => {
    const t = debounceRefs.current[key];
    if (t) clearTimeout(t);
    debounceRefs.current[key] = setTimeout(fn, 300);
  };
  // Parse a numeric input to a value | null (blank → null; NaN → leave unchanged
  // by returning the sentinel undefined, which the caller skips).
  const parseNum = (s: string): number | null | undefined => {
    if (s.trim() === '') return null;
    const n = Number(s);
    return Number.isFinite(n) ? n : undefined;
  };

  const focusInput = (): void => dispatch({ type: 'SET_INPUT_MODE', mode: 'filter' });
  const blurInput = (): void => dispatch({ type: 'SET_INPUT_MODE', mode: null });

  // Defensive unmount cleanup: the popover unmounts conditionally
  // (convFiltersOpen && !isSearching). If it unmounts while a numeric input still
  // holds focus without firing blur, inputMode would stay 'filter' and keep
  // suppressing single-char reader hotkeys until the next focus/blur. Reset it.
  useEffect(() => () => { dispatch({ type: 'SET_INPUT_MODE', mode: null }); }, []);

  const toggleProject = (label: string): void => {
    const next = filters.projects.includes(label)
      ? filters.projects.filter((p) => p !== label)
      : [...filters.projects, label];
    patch({ projects: next });
  };

  // #278 Theme C — mirror toggleProject for the model-family multi-select.
  const toggleModel = (fam: string): void => {
    const next = filters.models.includes(fam)
      ? filters.models.filter((m) => m !== fam)
      : [...filters.models, fam];
    patch({ models: next });
  };

  return (
    <div
      className="conv-rail-filters"
      role="dialog"
      aria-label="Conversation filters"
      // #228 S4 D1 (Codex gate P1-2) — Escape closes the popover from any focused
      // field inside it (SET_CONV_FILTERS_OPEN { open:false }), and stopPropagation
      // keeps it from reaching the document keydown listener. The view-level global
      // Escape already gates on `inView` (which excludes convFiltersOpen), so this
      // makes Escape positively useful here rather than merely inert.
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: false });
        }
      }}
    >
      <section className="conv-rail-filters-sec">
        <div className="conv-rail-filters-label">Date (last activity)</div>
        <div className="conv-rail-filters-chips">
          {DATE_PRESETS.map((p) => (
            <button
              key={p.key}
              type="button"
              className={`conv-rail-filters-chip${filters.datePreset === p.key ? ' is-on' : ''}`}
              onClick={() => {
                const b = p.bounds(resolvedTz);
                patch({ dateFrom: b.from, dateTo: b.to, datePreset: p.key });
              }}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="conv-rail-filters-range">
          <label className="conv-rail-filters-field">
            <span>From</span>
            <input
              type="date"
              value={filters.dateFrom ?? ''}
              aria-label="Date from"
              onFocus={focusInput}
              onBlur={blurInput}
              onChange={(e) => patch({ dateFrom: e.target.value || null, datePreset: null })}
            />
          </label>
          <label className="conv-rail-filters-field">
            <span>To</span>
            <input
              type="date"
              value={filters.dateTo ?? ''}
              aria-label="Date to"
              onFocus={focusInput}
              onBlur={blurInput}
              onChange={(e) => patch({ dateTo: e.target.value || null, datePreset: null })}
            />
          </label>
        </div>
      </section>

      <section className="conv-rail-filters-sec">
        <div className="conv-rail-filters-label">Project</div>
        <div className="conv-rail-filters-projects">
          {facets.projects.length === 0 && (
            <div className="conv-rail-filters-empty">No projects.</div>
          )}
          {facets.projects.map((p) => (
            <label key={p.project_label} className="conv-rail-filters-proj">
              <input
                type="checkbox"
                checked={filters.projects.includes(p.project_label)}
                aria-label={p.project_label}
                onChange={() => toggleProject(p.project_label)}
              />
              <span className="conv-rail-filters-proj-name">{p.project_label}</span>
              <span className="conv-rail-filters-proj-count">{p.count}</span>
            </label>
          ))}
        </div>
      </section>

      <section className="conv-rail-filters-sec">
        <div className="conv-rail-filters-label">Model</div>
        <div className="conv-rail-filters-chips">
          {facets.models.length === 0 && (
            <div className="conv-rail-filters-empty">No models.</div>
          )}
          {facets.models.map((m) => (
            <button
              key={m.family}
              type="button"
              // Reuse the family palette (modelChipClass) on the filter chip so
              // Opus/Sonnet/Haiku/Fable carry their established colour.
              className={`conv-rail-filters-chip chip ${modelChipClass(m.family)}${filters.models.includes(m.family) ? ' is-on' : ''}`}
              aria-pressed={filters.models.includes(m.family)}
              onClick={() => toggleModel(m.family)}
            >
              {m.family} <span className="conv-rail-filters-proj-count">{m.count}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="conv-rail-filters-sec">
        <div className="conv-rail-filters-label">Cost (USD)</div>
        <div className="conv-rail-filters-chips">
          {COST_PRESETS.map((c) => (
            <button
              key={c}
              type="button"
              className={`conv-rail-filters-chip${filters.costMin === c ? ' is-on' : ''}`}
              onClick={() => patch({ costMin: c })}
            >
              ≥${c}
            </button>
          ))}
        </div>
        <div className="conv-rail-filters-range">
          <label className="conv-rail-filters-field">
            <span>Min</span>
            <input
              type="number"
              inputMode="decimal"
              min="0"
              step="0.01"
              value={costMinStr}
              aria-label="Min cost"
              placeholder="0"
              onFocus={focusInput}
              onBlur={blurInput}
              onChange={(e) => {
                setCostMinStr(e.target.value);
                const v = parseNum(e.target.value);
                if (v !== undefined) debounced('costMin', () => patch({ costMin: v }));
              }}
            />
          </label>
          <label className="conv-rail-filters-field">
            <span>Max</span>
            <input
              type="number"
              inputMode="decimal"
              min="0"
              step="0.01"
              value={costMaxStr}
              aria-label="Max cost"
              placeholder="∞"
              onFocus={focusInput}
              onBlur={blurInput}
              onChange={(e) => {
                setCostMaxStr(e.target.value);
                const v = parseNum(e.target.value);
                if (v !== undefined) debounced('costMax', () => patch({ costMax: v }));
              }}
            />
          </label>
        </div>
      </section>

      <section className="conv-rail-filters-sec">
        <div className="conv-rail-filters-label">Cache rebuilds</div>
        <div className="conv-rail-filters-chips">
          {REBUILD_PRESETS.map((r) => (
            <button
              key={r}
              type="button"
              className={`conv-rail-filters-chip${filters.rebuildMin === r ? ' is-on' : ''}`}
              onClick={() => patch({ rebuildMin: r })}
            >
              ≥{r}
            </button>
          ))}
          <label className="conv-rail-filters-field conv-rail-filters-field--inline">
            <span>≥</span>
            <input
              type="number"
              inputMode="numeric"
              min="0"
              step="1"
              value={rebuildStr}
              aria-label="Min cache rebuilds"
              placeholder="N"
              onFocus={focusInput}
              onBlur={blurInput}
              onChange={(e) => {
                setRebuildStr(e.target.value);
                const v = parseNum(e.target.value);
                if (v !== undefined) {
                  // Whole-number threshold; floor a stray decimal.
                  const n = v === null ? null : Math.floor(v);
                  debounced('rebuildMin', () => patch({ rebuildMin: n }));
                }
              }}
            />
          </label>
        </div>
      </section>

      <div className="conv-rail-filters-footer">
        <button
          type="button"
          className="conv-rail-filters-clear"
          onClick={() => dispatch({ type: 'CLEAR_CONVERSATION_FILTERS' })}
        >
          Clear all
        </button>
        <button
          type="button"
          className="conv-rail-filters-done"
          onClick={() => dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: false })}
        >
          Done
        </button>
      </div>
    </div>
  );
}
