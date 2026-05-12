// Option-form controls for the share modal (spec §6.2 anatomy, §6.3
// behavior, plan §M1.13).
//
// Pure controlled component: receives the current `ShareOptions` recipe
// and an onChange callback. No fetch, no store reads. The parent
// (ShareModal) owns the source of truth and threads it down to
// <PreviewPane> and <ActionBar> as well.
//
// Spec invariants:
//   - "Anon on export" is the SOLE anon control. The label is
//     user-facing; the underlying field `reveal_projects` is the
//     inverse (checked ⇒ reveal_projects=false). Preview always
//     reveals (the preview pane forces `reveal_projects=true` in its
//     fetch — see PreviewPane.tsx).
//   - `Period` defaults to the panel's "current focus" (e.g. Weekly →
//     current week). M1 surfaces the three kinds (current / previous /
//     custom). Custom shows two date inputs (HTML5 <input type="date">).
//   - `Project allowlist` is stubbed in M1 — the spec leaves the
//     real multi-select popover for a later milestone (no UX detail
//     beyond "multi-select popover"). The stub renders a disabled
//     button with a "Coming in M3+" tooltip; the underlying
//     ShareOptions.project_allowlist stays null (= include all).
import type { ShareOptions, SharePeriod } from './types';

interface Props {
  options: ShareOptions;
  onChange: (next: ShareOptions) => void;
}

export function Knobs({ options, onChange }: Props) {
  const patch = (override: Partial<ShareOptions>) =>
    onChange({ ...options, ...override });

  const patchPeriod = (override: Partial<SharePeriod>) =>
    onChange({ ...options, period: { ...options.period, ...override } });

  // Top-N: HTML number input. Empty / NaN → null (kernel default).
  // Otherwise coerce to a positive integer; client-side floor at 1 so
  // the kernel's 400 response on `top_n < 1` is never the first feedback
  // the user gets. The server still validates, so this is belt-only.
  const handleTopN = (raw: string) => {
    if (raw === '') {
      patch({ top_n: null });
      return;
    }
    const n = Number.parseInt(raw, 10);
    if (Number.isNaN(n)) return;
    patch({ top_n: Math.max(1, n) });
  };

  return (
    <div className="share-knobs">
      <div className="share-knob">
        <label htmlFor="share-knob-period">Period</label>
        <select
          id="share-knob-period"
          value={options.period.kind}
          onChange={(e) =>
            patchPeriod({ kind: e.target.value as SharePeriod['kind'] })
          }
        >
          <option value="current">This week</option>
          <option value="previous">Previous week</option>
          <option value="custom">Custom</option>
        </select>
        {options.period.kind === 'custom' ? (
          <div className="share-knob-period-custom">
            <label className="share-knob-sublabel">
              <span>Start</span>
              <input
                type="date"
                value={options.period.start?.slice(0, 10) ?? ''}
                onChange={(e) => patchPeriod({ start: e.target.value })}
                aria-label="Custom period start date"
              />
            </label>
            <label className="share-knob-sublabel">
              <span>End</span>
              <input
                type="date"
                value={options.period.end?.slice(0, 10) ?? ''}
                onChange={(e) => patchPeriod({ end: e.target.value })}
                aria-label="Custom period end date"
              />
            </label>
          </div>
        ) : null}
      </div>

      <div className="share-knob">
        <span className="share-knob-label">Theme</span>
        <div className="share-knob-radiogroup" role="radiogroup" aria-label="Theme">
          <label className="share-knob-radio">
            <input
              type="radio"
              name="share-theme"
              value="light"
              checked={options.theme === 'light'}
              onChange={() => patch({ theme: 'light' })}
            />
            <span>Light</span>
          </label>
          <label className="share-knob-radio">
            <input
              type="radio"
              name="share-theme"
              value="dark"
              checked={options.theme === 'dark'}
              onChange={() => patch({ theme: 'dark' })}
            />
            <span>Dark</span>
          </label>
        </div>
      </div>

      <div className="share-knob">
        <label htmlFor="share-knob-topn">Top-N</label>
        <input
          id="share-knob-topn"
          type="number"
          min={1}
          step={1}
          inputMode="numeric"
          value={options.top_n ?? ''}
          onChange={(e) => handleTopN(e.target.value)}
          aria-label="Top-N"
        />
      </div>

      <div className="share-knob">
        <span className="share-knob-label">Projects</span>
        {/*
          M1: the multi-select popover ships in a later milestone (spec
          §6.3 says "multi-select popover with real project names"). The
          underlying `project_allowlist` stays null (= all projects). The
          disabled button is a placeholder so the layout matches the §6.2
          ASCII diagram.
        */}
        <button
          type="button"
          className="share-knob-allowlist-stub"
          disabled
          aria-disabled="true"
          title="Project allowlist — coming in M3+"
        >
          All projects
        </button>
      </div>

      <div className="share-knob">
        <label className="share-knob-checkbox">
          <input
            type="checkbox"
            checked={options.show_chart}
            onChange={(e) => patch({ show_chart: e.target.checked })}
            aria-label="Include chart in export"
          />
          <span>Show chart</span>
        </label>
      </div>
      <div className="share-knob">
        <label className="share-knob-checkbox">
          <input
            type="checkbox"
            checked={options.show_table}
            onChange={(e) => patch({ show_table: e.target.checked })}
            aria-label="Include table in export"
          />
          <span>Show table</span>
        </label>
      </div>
      <div className="share-knob">
        <label className="share-knob-checkbox">
          {/*
            "Anon on export" is the user-facing label; internally it
            flips ShareOptions.reveal_projects. Checked ⇒ reveal=false
            (anonymize). Preview always reveals — see PreviewPane.tsx.
          */}
          <input
            type="checkbox"
            checked={!options.reveal_projects}
            onChange={(e) => patch({ reveal_projects: !e.target.checked })}
            aria-label="Anonymize project names on export"
          />
          <span>Anon on export</span>
        </label>
      </div>
    </div>
  );
}
