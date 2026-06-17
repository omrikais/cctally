import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useConversationFacets } from '../hooks/useConversationFacets';
import type { ConversationFilters } from '../types/conversation';

// Browse-list filters popover (filters spec §4). Holds all four filter axes —
// date (preset chips + from/to range), project (multi-select from the facets
// endpoint), cost (min/max + quick presets), and cache-rebuilds (threshold
// presets + custom ≥N). Filters apply LIVE (numeric/text inputs debounced
// ~300ms); the footer "Done" only closes the popover. The numeric/text inputs
// dispatch SET_INPUT_MODE on focus/blur so single-char reader hotkeys are
// suppressed while typing (Task 4's named-key End binding additionally gates on
// convFiltersOpen). Renders only while convFiltersOpen (the rail owns that gate).

// Format a Date's UTC Y/M/D as 'YYYY-MM-DD'. Month boundaries are computed
// UTC-safe (Date.UTC) so a host TZ never shifts which calendar month a preset
// lands on — the server resolves the day boundaries in display.tz.
function ymdUtc(d: Date): string {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, '0');
  const day = String(d.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

interface DatePreset { key: string; label: string; bounds: () => { from: string; to: string } }

// Preset boundary calculators (UTC-safe). `this-month`/`last-month` use the
// Date.UTC(y, m+1, 0) "last day of month" idiom; `last-7d` is a 7-day window
// ending today (inclusive).
const DATE_PRESETS: DatePreset[] = [
  {
    key: 'this-month', label: 'This month',
    bounds: () => {
      const now = new Date();
      const y = now.getUTCFullYear();
      const m = now.getUTCMonth();
      return { from: ymdUtc(new Date(Date.UTC(y, m, 1))), to: ymdUtc(new Date(Date.UTC(y, m + 1, 0))) };
    },
  },
  {
    key: 'last-month', label: 'Last month',
    bounds: () => {
      const now = new Date();
      const y = now.getUTCFullYear();
      const m = now.getUTCMonth();
      return { from: ymdUtc(new Date(Date.UTC(y, m - 1, 1))), to: ymdUtc(new Date(Date.UTC(y, m, 0))) };
    },
  },
  {
    key: 'last-7d', label: 'Last 7d',
    bounds: () => {
      const now = new Date();
      const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
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

  // Local mirror of the debounced numeric/text inputs so typing feels instant
  // while the store update (and therefore the refetch) is debounced ~300ms. The
  // displayed values follow the store on an external change (e.g. Clear all).
  const [costMinStr, setCostMinStr] = useState(filters.costMin?.toString() ?? '');
  const [costMaxStr, setCostMaxStr] = useState(filters.costMax?.toString() ?? '');
  const [rebuildStr, setRebuildStr] = useState(filters.rebuildMin?.toString() ?? '');
  useEffect(() => { setCostMinStr(filters.costMin?.toString() ?? ''); }, [filters.costMin]);
  useEffect(() => { setCostMaxStr(filters.costMax?.toString() ?? ''); }, [filters.costMax]);
  useEffect(() => { setRebuildStr(filters.rebuildMin?.toString() ?? ''); }, [filters.rebuildMin]);

  // One shared debounce timer for the numeric inputs. Cleared on unmount.
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (debounceRef.current) clearTimeout(debounceRef.current); }, []);
  const debounced = (fn: () => void): void => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(fn, 300);
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

  const toggleProject = (label: string): void => {
    const next = filters.projects.includes(label)
      ? filters.projects.filter((p) => p !== label)
      : [...filters.projects, label];
    patch({ projects: next });
  };

  return (
    <div className="conv-rail-filters" role="dialog" aria-label="Conversation filters">
      <section className="conv-rail-filters-sec">
        <div className="conv-rail-filters-label">Date (last activity)</div>
        <div className="conv-rail-filters-chips">
          {DATE_PRESETS.map((p) => (
            <button
              key={p.key}
              type="button"
              className={`conv-rail-filters-chip${filters.datePreset === p.key ? ' is-on' : ''}`}
              onClick={() => {
                const b = p.bounds();
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
                if (v !== undefined) debounced(() => patch({ costMin: v }));
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
                if (v !== undefined) debounced(() => patch({ costMax: v }));
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
                  debounced(() => patch({ rebuildMin: n }));
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
