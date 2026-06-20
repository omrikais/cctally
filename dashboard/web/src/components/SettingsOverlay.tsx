import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import {
  dispatch,
  getState,
  selectMarkersEnabled,
  selectLiveTailEnabled,
  subscribeStore,
  SESSION_SORT_KEYS,
  type SessionSortKey,
} from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useKeymap } from '../hooks/useKeymap';
import { useModalFocus } from '../hooks/useModalFocus';
import { useScrollLock } from '../hooks/useScrollLock';
import type {
  AlertAxis,
  AlertsSettingsEnvelope,
  ProjectedMetric,
} from '../types/envelope';
import { AXIS_TITLE_LABEL } from '../lib/alertAxis';
import { ModalHeader } from '../modals/ModalHeader';

// Notifier dispatch backends (Phase B). The union mirrors
// `AlertsSettingsEnvelope.notifier` (single source of truth) so the dropdown
// can't drift from the wire contract. `NonNullable` strips the `?` so the
// local `useState` defaults cleanly to 'auto'.
type NotifierKind = NonNullable<AlertsSettingsEnvelope['notifier']>;

// Projected-axis metric sub-select labels (issue #121). The projected test
// alert mirrors the CLI's `--metric {weekly_pct,budget_usd}`: a single
// "Projected" axis option can't say WHICH projection to fire, so when that
// axis is picked we surface this secondary chooser and post `metric` too.
const PROJECTED_METRIC_LABEL: Record<ProjectedMetric, string> = {
  weekly_pct: 'Weekly %',
  budget_usd: 'Budget $',
  codex_budget_usd: 'Codex $',
};

// IANA-zone validator. `Intl.DateTimeFormat` throws RangeError on
// unknown zones; we treat that as the negative answer rather than
// trying to maintain a static allowlist (which would drift as tzdata
// updates ship). Empty string is invalid by definition (gives the user
// a clear "type something" target on the Custom row).
function isValidIANA(value: string): boolean {
  if (!value) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value }).format(new Date());
    return true;
  } catch {
    return false;
  }
}

// "America/New_York (GMT-04:00)" — preview of how the offset will read
// for a Custom-zone candidate, derived locally so the preview updates
// before Save round-trips through POST /api/settings + SSE rebroadcast.
// Offset comes from Intl's `shortOffset` partspec (returns "GMT-04:00"
// or similar). Falls back to the bare zone name on Intl errors.
function previewOffset(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, timeZoneName: 'shortOffset',
    }).formatToParts(new Date());
    const off = parts.find((p) => p.type === 'timeZoneName')?.value ?? '';
    return `${tz} (${off})`;
  } catch {
    return tz;
  }
}

type TzMode = 'local' | 'utc' | 'custom';

function modeFromTz(tz: string): TzMode {
  if (tz === 'local') return 'local';
  if (tz === 'utc') return 'utc';
  return 'custom';
}

// `s` opens; Save / Reset / Cancel buttons close; backdrop click or Esc
// also close.

export function SettingsOverlay() {
  const [open, setOpen] = useState(false);
  const prefs = useSyncExternalStore(subscribeStore, () => getState().prefs);
  const [sort, setSort] = useState<SessionSortKey>(prefs.sortDefault);
  const [perPage, setPerPage] = useState(prefs.sessionsPerPage);
  const filterTerm = useSyncExternalStore(subscribeStore, () => getState().filterText);
  const [filter, setFilter] = useState(filterTerm);

  // TZ subform state (hoisted from the former TzSection so the unified
  // bottom Save can commit it together with the localStorage-backed
  // prefs). Without unification, the bottom Save silently dropped TZ
  // changes — two Save buttons in one modal where one ignores half the
  // form is a textbook usability trap.
  const display = useDisplayTz();
  const [tzMode, setTzMode] = useState<TzMode>(modeFromTz(display.tz));
  const [tzCustom, setTzCustom] = useState<string>(
    modeFromTz(display.tz) === 'custom' ? display.tz : '',
  );
  const [tzSubmitting, setTzSubmitting] = useState(false);
  const [tzError, setTzError] = useState<string | null>(null);

  // Alerts subform (T9). Toggle bound to alertsConfig.enabled mirrored
  // from the snapshot envelope. Combined-save: when this and TZ are
  // both dirty the Save handler emits a single POST with both blocks
  // — required so the body shape matches T7's API and so users don't
  // pay two round-trips.
  const alertsConfig = useSyncExternalStore(
    subscribeStore,
    () => getState().alertsConfig,
  );
  const [alertsEnabled, setAlertsEnabled] = useState<boolean>(alertsConfig.enabled);
  // Projected axis (issue #121): two opt-in toggles. `projected_weekly`
  // routes to `alerts.projected_enabled`; `projected_budget` routes to
  // `budget.projected_enabled` in the POST /api/settings body.
  const [projectedWeekly, setProjectedWeekly] = useState<boolean>(
    alertsConfig.projected_weekly_enabled ?? false,
  );
  const [projectedBudget, setProjectedBudget] = useState<boolean>(
    alertsConfig.projected_budget_enabled ?? false,
  );
  // Per-project budget axis (issue #19/#121): single opt-in toggle that
  // routes to `budget.project_alerts_enabled` in the POST /api/settings body
  // (its own config block, same as the budget-projected toggle).
  const [projectAlerts, setProjectAlerts] = useState<boolean>(
    alertsConfig.project_alerts_enabled ?? false,
  );
  // Codex budget toggles (#134): two dashboard-writable sub-leaves of the
  // nested `budget.codex` block. `codexBudgetAlerts` → `budget.codex.alerts_enabled`;
  // `codexProjected` → `budget.codex.projected_enabled`. Both disabled (and
  // an empty-state hint shown) when no Codex budget exists
  // (`codex_budget_configured`, Q2) — amounts stay CLI-only.
  const [codexBudgetAlerts, setCodexBudgetAlerts] = useState<boolean>(
    alertsConfig.codex_budget_alerts_enabled ?? false,
  );
  const [codexProjected, setCodexProjected] = useState<boolean>(
    alertsConfig.codex_projected_enabled ?? false,
  );
  // Notifier dispatch backend (Phase B). Seeds from the SSE-mirrored
  // `alerts_settings.notifier` (default 'auto' when the envelope predates
  // the field). `command_configured` is a server-side boolean — the raw
  // `command_template` is NEVER sent to the client, so the "Custom command"
  // option is only selectable when the server reports a template is set.
  const [notifier, setNotifier] = useState<NotifierKind>(
    alertsConfig.notifier ?? 'auto',
  );
  // cache-failure-markers spec §5 — the conversation-viewer cache-rebuild
  // marker opt-out. Seeds from the SSE-mirrored dashboard_prefs slice (markers
  // ON by default), dirties independently, and travels in the combined Save
  // POST as `dashboard: { cache_failure_markers }`. selectMarkersEnabled does
  // the absent-field defaulting so the toggle never reads `undefined`.
  const markersEnabledServer = useSyncExternalStore(subscribeStore, () =>
    selectMarkersEnabled(getState()),
  );
  const [cacheMarkers, setCacheMarkers] = useState<boolean>(markersEnabledServer);
  // live-tail spec §4.2 — the conversation-viewer live-tail opt-out. Same
  // plumbing as cacheMarkers: seeds from the SSE-mirrored dashboard_prefs slice
  // (live-tail ON by default), dirties independently, and travels in the SAME
  // combined Save POST's `dashboard` block. selectLiveTailEnabled does the
  // absent-field defaulting so the toggle never reads `undefined`.
  const liveTailServer = useSyncExternalStore(subscribeStore, () =>
    selectLiveTailEnabled(getState()),
  );
  const [liveTail, setLiveTail] = useState<boolean>(liveTailServer);
  const [testSubmitting, setTestSubmitting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  // #207 D4: inline success confirmation for the happy path (dispatch ===
  // 'queued'), so the only feedback isn't the auto-dismissing alert toast.
  const [testOk, setTestOk] = useState(false);
  const [testAxis, setTestAxis] = useState<AlertAxis>('weekly');
  // Only consulted when testAxis === 'projected' (mirrors the CLI's
  // `alerts test --axis projected --metric`); ignored for other axes.
  const [testMetric, setTestMetric] = useState<ProjectedMetric>('weekly_pct');

  // Re-seed the local form whenever the server-side display.tz changes
  // (an SSE tick from another tab's Save, or a `cctally config` write
  // landing while Settings is open). Without this, the radio would
  // appear stuck at the prior selection and Save would re-POST identical
  // bytes.
  useEffect(() => {
    setTzMode(modeFromTz(display.tz));
    setTzCustom(modeFromTz(display.tz) === 'custom' ? display.tz : '');
  }, [display.tz]);

  // Re-seed the alerts toggle whenever the server-side alertsConfig
  // changes (the SSE tick after another tab's Save lands, or the
  // background T15 wire-up applies a fresh envelope). Same pattern as
  // the TZ re-seed above: without it the toggle would appear stuck.
  useEffect(() => {
    setAlertsEnabled(alertsConfig.enabled);
    setProjectedWeekly(alertsConfig.projected_weekly_enabled ?? false);
    setProjectedBudget(alertsConfig.projected_budget_enabled ?? false);
    setProjectAlerts(alertsConfig.project_alerts_enabled ?? false);
    setCodexBudgetAlerts(alertsConfig.codex_budget_alerts_enabled ?? false);
    setCodexProjected(alertsConfig.codex_projected_enabled ?? false);
    setNotifier(alertsConfig.notifier ?? 'auto');
  }, [
    alertsConfig.enabled,
    alertsConfig.projected_weekly_enabled,
    alertsConfig.projected_budget_enabled,
    alertsConfig.project_alerts_enabled,
    alertsConfig.codex_budget_alerts_enabled,
    alertsConfig.codex_projected_enabled,
    alertsConfig.notifier,
  ]);

  // cache-failure-markers spec §5 — re-seed the markers toggle on each SSE tick
  // (same pattern as the TZ/alerts re-seed above): a server flip (CLI write /
  // another tab's Save) flows through dashboard_prefs and repaints the toggle.
  useEffect(() => {
    setCacheMarkers(markersEnabledServer);
  }, [markersEnabledServer]);

  // live-tail spec §4.2 — re-seed the live-tail toggle on each SSE tick (same
  // pattern as the markers re-seed above): a server flip (CLI write / another
  // tab's Save) flows through dashboard_prefs and repaints the toggle.
  useEffect(() => {
    setLiveTail(liveTailServer);
  }, [liveTailServer]);

  useKeymap([
    // Parity with main's settings.js#152: don't stack Settings under an
    // open modal. Without this guard, pressing `s` over a modal opens
    // Settings hidden behind it and only becomes visible after the user
    // Escapes out of the front dialog.
    {
      key: 's',
      scope: 'global',
      view: 'any',     // all-views chrome (#156)
      action: () => setOpen(true),
      when: () => !getState().openModal,
    },
    // Esc at `modal` scope (z-index 100): SCOPE_ORDER beats the conversations
    // `global` Esc deterministically (#156).
    { key: 'Escape', scope: 'modal', action: () => setOpen(false), when: () => open },
    // While Settings is open, swallow the digit modal-openers so they don't
    // mount a dashboard modal on top of the overlay. `0` (the 10th-panel
    // opener) MUST be swallowed too (#156): otherwise it opens the alerts
    // modal over Settings, and the modal-scope Esc tie strands it.
    { key: '0', scope: 'modal', action: () => {}, when: () => open },
    { key: '1', scope: 'modal', action: () => {}, when: () => open },
    { key: '2', scope: 'modal', action: () => {}, when: () => open },
    { key: '3', scope: 'modal', action: () => {}, when: () => open },
    { key: '4', scope: 'modal', action: () => {}, when: () => open },
    { key: '5', scope: 'modal', action: () => {}, when: () => open },
    { key: '6', scope: 'modal', action: () => {}, when: () => open },
    { key: '7', scope: 'modal', action: () => {}, when: () => open },
    { key: '8', scope: 'modal', action: () => {}, when: () => open },
    { key: '9', scope: 'modal', action: () => {}, when: () => open },
  ]);

  // Fix I1: resync form state to current prefs every time the overlay opens.
  useEffect(() => {
    if (open) {
      setSort(prefs.sortDefault);
      setPerPage(prefs.sessionsPerPage);
      setFilter(filterTerm);
      setAlertsEnabled(alertsConfig.enabled);
      setProjectedWeekly(alertsConfig.projected_weekly_enabled ?? false);
      setProjectedBudget(alertsConfig.projected_budget_enabled ?? false);
      setProjectAlerts(alertsConfig.project_alerts_enabled ?? false);
      setCodexBudgetAlerts(alertsConfig.codex_budget_alerts_enabled ?? false);
      setCodexProjected(alertsConfig.codex_projected_enabled ?? false);
      setNotifier(alertsConfig.notifier ?? 'auto');
      setCacheMarkers(markersEnabledServer);
      setTestError(null);
      setTestAxis('weekly');
    }
  }, [
    open,
    prefs.sortDefault,
    prefs.sessionsPerPage,
    filterTerm,
    alertsConfig.enabled,
    alertsConfig.projected_weekly_enabled,
    alertsConfig.projected_budget_enabled,
    alertsConfig.project_alerts_enabled,
    alertsConfig.codex_budget_alerts_enabled,
    alertsConfig.codex_projected_enabled,
    alertsConfig.notifier,
    markersEnabledServer,
  ]);

  // a11y focus management (#207 A1). Settings is a local-state surface; it is
  // mutually exclusive with a panel modal (the `s` keybinding is guarded by
  // `!openModal`), so `trapEnabled` defaults to true and the contains-guard in
  // `useModalFocus` handles any Help-over-Settings case. Called BEFORE the
  // `!open` early-return so the hook order stays stable (Rules of Hooks).
  const cardRef = useRef<HTMLDivElement>(null);
  useModalFocus(cardRef, { active: open });

  // M1-1: lock background page scroll while Settings is open. Declared
  // BEFORE the `!open` early-return so the hook order stays stable.
  useScrollLock(open);

  // #207 D2: while Settings is open, the always-on hotkeys (digits, r/q/n/N,
  // c/S/B/f//) must be inert. Settings is component-local and invisible to the
  // store's modal fields, so it explicitly tracks itself via a depth counter.
  // Declared BEFORE the `!open` early-return so the hook order stays stable.
  useEffect(() => {
    if (!open) return;
    dispatch({ type: 'INCREMENT_CHROME_OVERLAY' });
    return () => dispatch({ type: 'DECREMENT_CHROME_OVERLAY' });
  }, [open]);

  if (!open) return null;

  const tzTargetValue =
    tzMode === 'local' ? 'local'
    : tzMode === 'utc' ? 'utc'
    : tzCustom.trim();
  const tzCustomValid = tzMode !== 'custom' || isValidIANA(tzCustom.trim());
  const tzDirty = tzTargetValue !== display.tz;
  const alertsDirty = alertsEnabled !== alertsConfig.enabled;
  // Projected toggles dirty independently — `alerts.projected_enabled`
  // travels in the `alerts` block, `budget.projected_enabled` in `budget`.
  const projectedWeeklyDirty =
    projectedWeekly !== (alertsConfig.projected_weekly_enabled ?? false);
  const projectedBudgetDirty =
    projectedBudget !== (alertsConfig.projected_budget_enabled ?? false);
  // Per-project budget toggle dirty — `budget.project_alerts_enabled` travels
  // in the `budget` block (issue #19/#121), alongside the budget-projected
  // toggle.
  const projectAlertsDirty =
    projectAlerts !== (alertsConfig.project_alerts_enabled ?? false);
  // Codex budget toggles (#134) dirty independently — both travel nested under
  // `budget.codex` (partial-merge; only the dirty sub-leaf is sent).
  const codexBudgetAlertsDirty =
    codexBudgetAlerts !== (alertsConfig.codex_budget_alerts_enabled ?? false);
  const codexProjectedDirty =
    codexProjected !== (alertsConfig.codex_projected_enabled ?? false);
  // cache-failure-markers spec §5 — dirty against the SSE-mirrored server value.
  // Travels in its OWN `dashboard` block on the combined Save POST.
  const cacheMarkersDirty = cacheMarkers !== markersEnabledServer;
  // live-tail spec §4.2 — dirty against the SSE-mirrored server value. Rides the
  // SAME `dashboard` block as cacheMarkers (both leaves, one block).
  const liveTailDirty = liveTail !== liveTailServer;
  // Notifier (Phase B): dirty against the mirrored value (default 'auto').
  // `commandConfigured` gates the "Custom command" option — when the server
  // has no `command_template`, picking 'command' would dispatch nothing, so
  // the option is disabled.
  const notifierDirty = notifier !== (alertsConfig.notifier ?? 'auto');
  const commandConfigured = alertsConfig.command_configured ?? false;
  // Save is gated when TZ is dirty-but-invalid (custom mode with
  // unparseable zone) or while a server-side POST is in flight. Non-TZ
  // dispatches are synchronous local-state updates and can't fail, so
  // they never gate Save.
  const saveDisabled = tzSubmitting || (tzDirty && tzMode === 'custom' && !tzCustomValid);

  const close = () => setOpen(false);
  const save = async () => {
    // 1. If any server-persisted block is dirty, commit it via POST
    //    /api/settings BEFORE dispatching local prefs. Combined body
    //    shape `{display?, alerts?}`: we send only the dirty block(s)
    //    in a single round-trip so users don't pay two POSTs and the
    //    server applies both atomically. TZ pin (--tz flag) suppresses
    //    only the display block, not alerts.
    const body: Record<string, unknown> = {};
    if (tzDirty && !display.pinned) {
      if (!tzCustomValid) {
        setTzError('Invalid IANA zone');
        return;
      }
      body.display = { tz: tzTargetValue };
    }
    // The `alerts` block carries `enabled` (master), the weekly-projected
    // toggle (`alerts.projected_enabled`), and the notifier backend
    // (`alerts.notifier`); send only the dirty sub-keys.
    if (alertsDirty || projectedWeeklyDirty || notifierDirty) {
      const alertsBlock: Record<string, unknown> = {};
      if (alertsDirty) alertsBlock.enabled = alertsEnabled;
      if (projectedWeeklyDirty) alertsBlock.projected_enabled = projectedWeekly;
      if (notifierDirty) alertsBlock.notifier = notifier;
      body.alerts = alertsBlock;
    }
    // The budget-projected toggle AND the per-project budget toggle both live
    // in the OWN `budget` config block — separate from `alerts` (issue
    // #19/#121). Merge whichever are dirty into a single `budget` block so the
    // server applies them in one atomic write (and a single reconcile pass).
    if (projectedBudgetDirty || projectAlertsDirty) {
      const budgetBlock: Record<string, unknown> = {};
      if (projectedBudgetDirty) budgetBlock.projected_enabled = projectedBudget;
      if (projectAlertsDirty) {
        budgetBlock.project_alerts_enabled = projectAlerts;
      }
      body.budget = budgetBlock;
    }
    // Codex budget toggles (#134): the two dashboard-writable sub-leaves nest
    // under `budget.codex` so the server's nested partial-merge writer updates
    // them WITHOUT clobbering the sibling amount_usd/period/alert_thresholds.
    // Send only the dirty sub-leaf(s); attach under the SAME `budget` block as
    // any flat Claude leaves above (so a one-shot Save carries both). No POST
    // when neither Codex toggle is dirty.
    if (codexBudgetAlertsDirty || codexProjectedDirty) {
      const codexBlock: Record<string, unknown> = {};
      if (codexBudgetAlertsDirty) codexBlock.alerts_enabled = codexBudgetAlerts;
      if (codexProjectedDirty) codexBlock.projected_enabled = codexProjected;
      body.budget = {
        ...((body.budget as Record<string, unknown> | undefined) ?? {}),
        codex: codexBlock,
      };
    }
    // cache-failure-markers spec §5 + live-tail spec §4.2 — the two
    // dashboard-scoped opt-outs ride ONE shared `dashboard` block in the SAME
    // combined POST (one round-trip, applied atomically server-side). Each leaf
    // is sent only when its own toggle is dirty.
    if (cacheMarkersDirty || liveTailDirty) {
      body.dashboard = {
        ...(cacheMarkersDirty ? { cache_failure_markers: cacheMarkers } : {}),
        ...(liveTailDirty ? { live_tail: liveTail } : {}),
      };
    }
    if (Object.keys(body).length > 0) {
      setTzSubmitting(true);
      setTzError(null);
      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          const errBody = (await res.json().catch(() => ({}))) as { error?: string };
          throw new Error(errBody.error ?? `HTTP ${res.status}`);
        }
        // No optimistic UI: the F2 SSE broadcast arrives within ~100ms
        // and updates display.* / alertsConfig.* via the snapshot
        // store; the useEffects above re-seed the form from the new
        // server values.
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'unknown error';
        setTzError(msg);
        setTzSubmitting(false);
        return;
      }
      setTzSubmitting(false);
    }

    // 2. Commit non-TZ prefs (existing logic).
    //    Clamp to the input's declared range. Bare min/max only validates on
    //    form submit; an empty input becomes Number('') === 0, which would
    //    empty the Sessions panel until Reset.
    const safePerPage =
      Number.isFinite(perPage) && perPage > 0
        ? Math.min(1000, Math.max(10, Math.round(perPage)))
        : prefs.sessionsPerPage;
    dispatch({ type: 'SAVE_PREFS', patch: { sortDefault: sort, sessionsPerPage: safePerPage } });
    dispatch({ type: 'SET_SORT', key: sort });
    dispatch({ type: 'SET_FILTER', text: filter });
    // Clear the sessions header-click override so the freshly-saved Sort
    // default actually takes effect. Trend has no Settings-side default —
    // leave its override untouched.
    dispatch({ type: 'SET_TABLE_SORT', table: 'sessions', override: null });
    close();
  };
  const reset = () => {
    // Reset clears localStorage-backed prefs only; display.tz is
    // server-persisted and intentionally unchanged here. Reverting it
    // would require a second POST and likely surprise users who only
    // wanted to clear sort / filter / per-page.
    dispatch({ type: 'RESET_PREFS' });
    dispatch({ type: 'SET_FILTER', text: '' });
    close();
  };

  return (
    <div id="settings-root">
      <div className="modal-backdrop" onClick={close} />
      <div
        ref={cardRef}
        className="modal-card accent-orange"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
      >
        <ModalHeader title="Settings" titleId="settings-title" onClose={close} />
        <div className="modal-body">
          <fieldset className="settings-fs">
            <legend>Display timezone</legend>
            {display.pinned && (
              <small>Pinned by --tz flag — restart the server without --tz to change here.</small>
            )}
            <label>
              <input
                type="radio"
                name="tz-mode"
                value="local"
                checked={tzMode === 'local'}
                onChange={() => setTzMode('local')}
                disabled={display.pinned}
              />{' '}
              Local ({display.resolvedTz})
            </label>
            <label>
              <input
                type="radio"
                name="tz-mode"
                value="utc"
                checked={tzMode === 'utc'}
                onChange={() => setTzMode('utc')}
                disabled={display.pinned}
              />{' '}
              UTC
            </label>
            <label>
              <input
                type="radio"
                name="tz-mode"
                value="custom"
                checked={tzMode === 'custom'}
                onChange={() => setTzMode('custom')}
                disabled={display.pinned}
              />{' '}
              Custom:{' '}
              <input
                type="text"
                value={tzCustom}
                onChange={(e) => setTzCustom(e.target.value)}
                disabled={tzMode !== 'custom' || display.pinned}
                placeholder="America/New_York"
                aria-invalid={tzMode === 'custom' && !tzCustomValid}
              />
            </label>
            {tzMode === 'custom' && tzCustom.trim() && (
              tzCustomValid
                ? <small>resolves to: {previewOffset(tzCustom.trim())}</small>
                : <div className="modal-error">Invalid IANA zone</div>
            )}
            {tzError && <div className="modal-error">Failed: {tzError}</div>}
          </fieldset>
          <fieldset className="settings-fs alerts-fs">
            <legend>Alerts</legend>
            <label>
              <input
                type="checkbox"
                name="alerts-enabled"
                checked={alertsEnabled}
                onChange={(e) => setAlertsEnabled(e.target.checked)}
              />{' '}
              Enable threshold alerts
            </label>
            {/*
              Notifier backend selector (Phase B). Seeded from the
              SSE-mirrored `alerts_settings.notifier`. The "Custom command"
              option is disabled unless the server reports a configured
              `command_template` (`command_configured`) — the raw template is
              never sent to the client, so the dashboard can only SELECT the
              command notifier, not author it. The hint line surfaces that
              the template is edited via the CLI.
            */}
            <label className="settings-row">
              Notifier{' '}
              <select
                className="settings-btn settings-select"
                value={notifier}
                aria-label="Alert notifier"
                onChange={(e) => setNotifier(e.target.value as NotifierKind)}
              >
                <option value="auto">Auto-detect</option>
                <option value="osascript">macOS (osascript)</option>
                <option value="notify-send">Linux (notify-send)</option>
                <option value="command" disabled={!commandConfigured}>
                  Custom command{commandConfigured ? '' : ' (set via CLI)'}
                </option>
                <option value="none">None (log only)</option>
              </select>
            </label>
            {commandConfigured && (
              <p className="settings-hint">
                Custom command configured (edit via CLI).
              </p>
            )}
            {/*
              Spec §8.1 — read-only summary of the active threshold lists.
              Sourced from
              `state.alertsConfig.{weekly,five_hour,budget}_thresholds`,
              which the SSE handler keeps mirrored from the envelope each
              tick (INGEST_SNAPSHOT_ALERTS reducer). v1 has no editor; the
              user mutates these via `cctally config set
              alerts.weekly_thresholds …` (and `budget.alert_thresholds`
              for the budget axis) and the new values flow back through
              this line on the next snapshot. Budget is its OWN config
              block (issue #19), so its thresholds come from
              `alertsConfig.budget_thresholds`, not the alerts block.
            */}
            <p className="alerts-summary settings-hint">
              Weekly: {alertsConfig.weekly_thresholds.map((t) => `${t}%`).join(', ')}
              {' · '}
              5h-block: {alertsConfig.five_hour_thresholds.map((t) => `${t}%`).join(', ')}
              {' · '}
              Budget: {(alertsConfig.budget_thresholds ?? []).map((t) => `${t}%`).join(', ') || '—'}
            </p>
            {/*
              Projected-pace toggles (issue #121). Two independent opt-ins,
              both default OFF, each gated server-side behind its parent
              axis's master switch. `projected_weekly` → `alerts.projected_enabled`;
              `projected_budget` → `budget.projected_enabled` (its own config
              block). Values mirror from alerts_settings each tick.
            */}
            <label>
              <input
                type="checkbox"
                name="projected-weekly-enabled"
                checked={projectedWeekly}
                onChange={(e) => setProjectedWeekly(e.target.checked)}
              />{' '}
              Projected weekly-% pace alerts
            </label>
            <label>
              <input
                type="checkbox"
                name="projected-budget-enabled"
                checked={projectedBudget}
                onChange={(e) => setProjectedBudget(e.target.checked)}
              />{' '}
              Projected budget-$ pace alerts
            </label>
            {/*
              Per-project budget alerts (issue #19/#121). A single opt-in,
              default OFF, routing to `budget.project_alerts_enabled` (its own
              config block). Gates push alerts only — the per-project display
              section in `cctally budget` always renders configured projects.
              Per-project budget AMOUNTS stay CLI-only (cwd-resolved); the
              dashboard only toggles the axis on/off.
            */}
            <label>
              <input
                type="checkbox"
                name="project-alerts-enabled"
                checked={projectAlerts}
                onChange={(e) => setProjectAlerts(e.target.checked)}
              />{' '}
              Per-project budget alerts
            </label>
            {/*
              Codex budget toggles (#134). Two dashboard-writable sub-leaves of
              the nested `budget.codex` block: `alerts_enabled` (actual-spend)
              and `projected_enabled` (projected-pace). Both DISABLED, and an
              empty-state hint shown, when no Codex budget exists
              (`codex_budget_configured`, Q2) — amounts stay CLI-only, and the
              disable structurally prevents the server's null-codex 400. The
              two toggles are independent in the UI; server-side, Codex
              projected requires alerts_enabled to fire (mirrors Claude), noted
              in budget.md rather than enforced as a cross-toggle dependency.
            */}
            <label>
              <input
                type="checkbox"
                name="codex-budget-alerts-enabled"
                checked={codexBudgetAlerts}
                disabled={!alertsConfig.codex_budget_configured}
                onChange={(e) => setCodexBudgetAlerts(e.target.checked)}
              />{' '}
              Codex budget alerts
            </label>
            <label>
              <input
                type="checkbox"
                name="codex-projected-enabled"
                checked={codexProjected}
                disabled={!alertsConfig.codex_budget_configured}
                onChange={(e) => setCodexProjected(e.target.checked)}
              />{' '}
              Codex projected-pace alerts
            </label>
            {!alertsConfig.codex_budget_configured && (
              <p className="settings-hint">
                Set a Codex budget via the CLI first:{' '}
                <code>cctally budget set 200 --vendor codex</code>
              </p>
            )}
            <div className="alerts-test-row">
              <label>
                Axis{' '}
                <select
                  className="settings-btn settings-select"
                  value={testAxis}
                  disabled={testSubmitting}
                  aria-label="Test alert axis"
                  onChange={(e) => setTestAxis(e.target.value as AlertAxis)}
                >
                  {(Object.keys(AXIS_TITLE_LABEL) as AlertAxis[]).map((ax) => (
                    <option key={ax} value={ax}>
                      {AXIS_TITLE_LABEL[ax]}
                    </option>
                  ))}
                </select>
              </label>{' '}
              {testAxis === 'projected' && (
                <label>
                  Metric{' '}
                  <select
                    className="settings-btn settings-select"
                    value={testMetric}
                    disabled={testSubmitting}
                    aria-label="Test alert projected metric"
                    onChange={(e) =>
                      setTestMetric(e.target.value as ProjectedMetric)
                    }
                  >
                    {(Object.keys(PROJECTED_METRIC_LABEL) as ProjectedMetric[]).map(
                      (m) => (
                        <option key={m} value={m}>
                          {PROJECTED_METRIC_LABEL[m]}
                        </option>
                      ),
                    )}
                  </select>
                </label>
              )}{' '}
              <button
                className="settings-btn"
                type="button"
                disabled={testSubmitting}
                onClick={async () => {
                  setTestSubmitting(true);
                  setTestError(null);
                  setTestOk(false);
                  try {
                    const res = await fetch('/api/alerts/test', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        axis: testAxis,
                        threshold: 90,
                        // metric only matters for the projected axis; the
                        // endpoint ignores it elsewhere, but keep the wire
                        // minimal and send it only when it applies.
                        ...(testAxis === 'projected'
                          ? { metric: testMetric }
                          : {}),
                      }),
                    });
                    const body = (await res.json().catch(() => ({}))) as {
                      dispatch?: string;
                      alert?: import('../types/envelope').AlertEntry;
                      reason?: string;
                    };
                    // CLAUDE.md "Test alerts deliberately diverge from real
                    // alerts": the dashboard endpoint returns the payload
                    // directly to the caller so a toast renders even when
                    // osascript fails. Decouple the toast dispatch from the
                    // dispatch status — show the toast whenever an alert
                    // payload is present; show the error message whenever
                    // dispatch is anything other than "queued". Both can
                    // surface simultaneously.
                    if (body.alert) {
                      dispatch({ type: 'SHOW_ALERT_TOAST', alert: body.alert });
                    }
                    if (body.dispatch === 'queued') {
                      setTestOk(true);
                    } else {
                      setTestError(body.dispatch ?? body.reason ?? `HTTP ${res.status}`);
                    }
                  } catch (e) {
                    setTestError(e instanceof Error ? e.message : 'unknown error');
                  }
                  setTestSubmitting(false);
                }}
              >
                {testSubmitting ? 'Sending…' : 'Send test alert'}
              </button>
              <p className="settings-hint">
                Sends a synthetic alert through the dispatch pipeline so you
                can verify the toast and log path. Does not write to the
                database or update the Recent alerts panel. Independent of
                the Enabled toggle — fire-and-test before flipping production
                alerts on.
              </p>
              {testError && (
                <div className="modal-error">Test failed: {testError}</div>
              )}
              {testOk && (
                <div className="settings-ok">Test alert dispatched ✓</div>
              )}
            </div>
          </fieldset>
          {/*
            cache-failure-markers spec §5 — the conversation-viewer cache-rebuild
            marker opt-out. One checkbox, default checked (markers ON), dirtying
            independently and committed in the single combined Save POST as
            `dashboard: { cache_failure_markers }`.
          */}
          <fieldset className="settings-fs">
            <legend>Conversation viewer</legend>
            <label>
              <input
                type="checkbox"
                name="cache-failure-markers"
                checked={cacheMarkers}
                onChange={(e) => setCacheMarkers(e.target.checked)}
              />{' '}
              Show cache-failure markers
            </label>
            <p className="settings-hint">
              Marks assistant turns that re-created the bulk of their cached
              prefix instead of reading it (a cost inefficiency, usually after an
              idle gap past the cache TTL). On by default.
            </p>
            <label>
              <input
                type="checkbox"
                name="live-tail"
                checked={liveTail}
                onChange={(e) => setLiveTail(e.target.checked)}
              />{' '}
              Live-tail new turns
            </label>
            <p className="settings-hint">
              Fetch new turns the instant the session's file changes (instead of
              waiting for the periodic refresh). On by default.
            </p>
          </fieldset>
          <fieldset className="settings-fs">
            <legend>Sort default</legend>
            {SESSION_SORT_KEYS.map(({ key, label }) => (
              <label key={key}>
                <input
                  type="radio"
                  name="sort-default"
                  value={key}
                  checked={sort === key}
                  onChange={() => setSort(key)}
                />{' '}
                {label}
              </label>
            ))}
          </fieldset>
          <fieldset className="settings-fs">
            <legend>Remembered filter term</legend>
            <input
              type="text"
              placeholder="(none)"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </fieldset>
          <fieldset className="settings-fs">
            <legend>Sessions per page</legend>
            <input
              type="number"
              min={10}
              max={1000}
              value={perPage}
              onChange={(e) => setPerPage(Number(e.target.value))}
            />
          </fieldset>
          <fieldset className="settings-fs sorting-fs">
            <legend>Table sorting</legend>
            <button
              className="settings-btn"
              type="button"
              disabled={!prefs.trendSortOverride && !prefs.sessionsSortOverride}
              onClick={() => {
                dispatch({ type: 'CLEAR_TABLE_SORTS' });
                close();
              }}
            >
              Reset table sorting
            </button>
            <p className="settings-hint">
              Clears column-click sorting on the $/1% Trend and Recent Sessions tables.
            </p>
          </fieldset>
          <fieldset className="settings-fs layout-fs">
            <legend>Layout</legend>
            <button
              className="settings-btn"
              type="button"
              onClick={() => {
                dispatch({ type: 'RESET_PANEL_ORDER' });
                close();
              }}
            >
              Reset card order
            </button>
          </fieldset>
          <div className="settings-actions">
            <button
              className="settings-btn"
              type="button"
              onClick={save}
              disabled={saveDisabled}
            >
              {tzSubmitting ? 'Saving…' : 'Save'}
            </button>
            <button className="settings-btn" type="button" onClick={reset}>
              Reset view preferences
            </button>
            <button className="settings-btn" type="button" onClick={close}>
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
