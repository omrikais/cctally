import { useEffect, useState, useSyncExternalStore } from 'react';
import {
  dispatch,
  getState,
  subscribeStore,
  SESSION_SORT_KEYS,
  type SessionSortKey,
} from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useKeymap } from '../hooks/useKeymap';

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
  const [testSubmitting, setTestSubmitting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);

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
  }, [alertsConfig.enabled]);

  useKeymap([
    // Parity with main's settings.js#152: don't stack Settings under an
    // open modal. Without this guard, pressing `s` over a modal opens
    // Settings hidden behind it and only becomes visible after the user
    // Escapes out of the front dialog.
    {
      key: 's',
      scope: 'global',
      action: () => setOpen(true),
      when: () => !getState().openModal,
    },
    { key: 'Escape', scope: 'global', action: () => setOpen(false), when: () => open },
    // While Settings is open, swallow the digit modal-openers so they
    // don't mount a dashboard modal on top of the overlay (parity with
    // main's settings.js #settings-root visibility guard). Modal scope
    // beats global in SCOPE_ORDER, so these run first.
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
      setTestError(null);
    }
  }, [open, prefs.sortDefault, prefs.sessionsPerPage, filterTerm, alertsConfig.enabled]);

  if (!open) return null;

  const tzTargetValue =
    tzMode === 'local' ? 'local'
    : tzMode === 'utc' ? 'utc'
    : tzCustom.trim();
  const tzCustomValid = tzMode !== 'custom' || isValidIANA(tzCustom.trim());
  const tzDirty = tzTargetValue !== display.tz;
  const alertsDirty = alertsEnabled !== alertsConfig.enabled;
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
    if (alertsDirty) {
      body.alerts = { enabled: alertsEnabled };
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
        className="modal-card accent-orange"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
      >
        <header className="modal-header">
          <h2 id="settings-title">Settings</h2>
          <button className="modal-close" type="button" aria-label="Close" onClick={close}>
            ×
          </button>
        </header>
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
              Spec §8.1 — read-only summary of the active threshold lists.
              Sourced from `state.alertsConfig.{weekly,five_hour}_thresholds`,
              which the SSE handler keeps mirrored from the envelope each
              tick (INGEST_SNAPSHOT_ALERTS reducer). v1 has no editor; the
              user mutates these via `cctally config set
              alerts.weekly_thresholds …` and the new values flow back
              through this line on the next snapshot.
            */}
            <p className="alerts-summary settings-hint">
              Weekly: {alertsConfig.weekly_thresholds.map((t) => `${t}%`).join(', ')}
              {' · '}
              5h-block: {alertsConfig.five_hour_thresholds.map((t) => `${t}%`).join(', ')}
            </p>
            <div className="alerts-test-row">
              <button
                className="settings-btn"
                type="button"
                disabled={testSubmitting}
                onClick={async () => {
                  setTestSubmitting(true);
                  setTestError(null);
                  try {
                    const res = await fetch('/api/alerts/test', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ axis: 'weekly', threshold: 90 }),
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
                    if (body.dispatch !== 'queued') {
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
            </div>
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
              Reset to defaults
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
