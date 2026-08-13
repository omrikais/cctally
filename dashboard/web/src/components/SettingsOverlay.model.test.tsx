// SettingsOverlay — the form MODEL: which fields are dirty, what the change
// count reads, and how a working-copy edit survives (or adopts) a concurrent
// server tick. #513 S2 consolidates the six settings suites into three; this
// file holds the cases that describe the reducer's contract rather than any
// particular control's markup, moved here verbatim from
// src/components/SettingsOverlay.test.tsx.
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsOverlay } from './SettingsOverlay';
import { _resetForTests, dispatch, getState } from '../store/store';
import type { AlertsConfig } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';

// Seed `state.alertsConfig` (the SSE-mirrored alerts_settings block) so the
// notifier dropdown reads the server-reported `notifier` / `command_configured`
// values. INGEST_SNAPSHOT_ALERTS replaces alertsConfig wholesale.
function seedAlertsConfig(patch: Partial<AlertsConfig>) {
  act(() => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: {
        enabled: false,
        weekly_thresholds: [90, 95],
        five_hour_thresholds: [90, 95],
        budget_thresholds: [90, 100],
        budget_enabled: false,
        projected_weekly_enabled: false,
        projected_budget_enabled: false,
        ...patch,
      },
      isFirstTick: true,
    });
  });
}

function openSettings() {
  // SettingsOverlay registers `{ key: 's', scope: 'global' }` via useKeymap
  // (see SettingsOverlay.tsx). The keymap module listens on `document`;
  // dispatching the keydown there mirrors the real user flow.
  fireEvent.keyDown(document, { key: 's' });
}

// cache-failure-markers toggle exactly: seeds from the SSE-mirrored
// dashboard_prefs slice (live-tail ON by default), dirties independently, and
// travels in the SAME combined Save POST's `dashboard` block as
// `dashboard: { live_tail }`. Re-seeds on SSE tick. One modal, one Save.
function seedLiveTailPref(live_tail: boolean) {
  act(() => {
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { live_tail } });
  });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymapForTests();
  // useKeymap only registers bindings — production wires the listener via
  // installGlobalKeydown(). Tests must attach it so the dispatched keydown
  // reaches the bound handler. (Same pattern as HelpOverlay.test.tsx.)
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
  vi.restoreAllMocks();
  // restoreAllMocks undoes spies, NOT vi.stubGlobal — the fetch stub must be
  // torn down explicitly or it leaks onto globalThis and contaminates later
  // tests in the worker (same cleanup as ProjectsModal.test.tsx / ActionBar).
  vi.unstubAllGlobals();
});

// SET-1 (#252): the unified deferred-commit form surfaces its pending-edit
// count on the Save button and disables Save when nothing is dirty — the
// missing "unsaved changes" feedback the issue called out. The Task-1 backbone
// also fixes the Codex blocker: an unrelated Save (e.g. only alerts.notifier)
// must NOT clobber the user's Recent-Sessions column-click sort.
describe('<SettingsOverlay /> dirty-state feedback (SET-1)', () => {
  it('Save reads plain "Save" and is disabled when nothing is dirty', () => {
    render(<SettingsOverlay />);
    openSettings();
    const save = screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    expect(save.textContent).toBe('Save');
    expect(save.disabled).toBe(true);
  });

  it('badges the change count and enables Save as fields dirty', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    const save = () => screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    expect(save().textContent).toBe('Save · 1 change');
    expect(save().disabled).toBe(false);
    // a second, different field (the Conversation-viewer live-tail toggle)
    fireEvent.click(screen.getByRole('checkbox', { name: /Live-tail new turns/ }));
    expect(save().textContent).toBe('Save · 2 changes');
    // revert both → back to disabled plain Save
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'auto' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /Live-tail new turns/ }));
    expect(save().textContent).toBe('Save');
    expect(save().disabled).toBe(true);
  });

  it('does NOT clear the sessions column sort when only a server field is saved', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    // Simulate a user-set Recent Sessions column sort override. SortOverride's
    // real shape is { column, direction } (src/lib/tableSort.ts) — the plan
    // skeleton's { key, dir } is not the store type.
    act(() =>
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'sessions',
        override: { column: 'cost', direction: 'desc' },
      }),
    );
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // The override the user never touched must survive an unrelated Save.
    expect(getState().prefs.sessionsSortOverride).toEqual({ column: 'cost', direction: 'desc' });
  });
});

// #258 — re-seed conflict guard. A concurrent same-field write via SSE while
// the overlay is open must NOT overwrite the user's pending edit to that field.
// Same-field keep is witnessed only on multi-value fields (notifier/TZ) — a
// boolean same-field test is vacuous (see the spec's witness-value caution).
describe('<SettingsOverlay /> re-seed conflict guard (#258)', () => {
  it('keeps a pending notifier edit when a concurrent same-field tick arrives', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    const select = screen.getByLabelText('Alert notifier') as HTMLSelectElement;
    expect(select.value).toBe('auto');
    // User edits the field → pending 'none'.
    fireEvent.change(select, { target: { value: 'none' } });
    expect(select.value).toBe('none');
    // A concurrent write (another tab / CLI) ticks the SAME field to a THIRD
    // value. 'keep' (stays 'none') and 'adopt' (becomes 'osascript') are
    // distinguishable.
    act(() => seedAlertsConfig({ notifier: 'osascript' }));
    expect(select.value).toBe('none'); // pending edit kept, not clobbered
    // Still dirty ('none' !== server 'osascript') → the Save badge persists.
    expect(
      (screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement).textContent,
    ).toBe('Save · 1 change');
  });

  it('keeps a pending boolean edit when an UNRELATED field ticks (on-open clobber site)', () => {
    render(<SettingsOverlay />);
    // live-tail server ON; also seed alerts so the unrelated tick is observable.
    seedLiveTailPref(true);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    const liveTail = () =>
      screen.getByRole('checkbox', { name: /Live-tail new turns/ }) as HTMLInputElement;
    // User flips live-tail OFF (dirty: false !== server true).
    fireEvent.click(liveTail());
    expect(liveTail().checked).toBe(false);
    // An UNRELATED mirrored field ticks. Against today's broad-deps on-open
    // effect this re-fires and hard-seeds live-tail back to ON — the second
    // clobber site. The guard must keep it OFF.
    act(() => seedAlertsConfig({ notifier: 'osascript' }));
    expect(liveTail().checked).toBe(false); // pending edit kept
  });

  it('adopts a concurrent write to a DIFFERENT field while holding a pending edit', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto', enabled: false });
    openSettings();
    const select = screen.getByLabelText('Alert notifier') as HTMLSelectElement;
    const master = () =>
      screen.getByRole('checkbox', { name: /Enable threshold alerts/ }) as HTMLInputElement;
    // Pending edit on notifier.
    fireEvent.change(select, { target: { value: 'none' } });
    expect(master().checked).toBe(false);
    // A concurrent write flips a DIFFERENT field (the master) ON. The seed
    // helper replaces alertsConfig wholesale, so re-send the pending-edit
    // field at its ORIGINAL server value ('auto') to model a single-field
    // external change.
    act(() => seedAlertsConfig({ notifier: 'auto', enabled: true }));
    expect(master().checked).toBe(true);  // untouched field adopted
    expect(select.value).toBe('none');    // pending edit still held
  });

  it('re-adopts after the user manually rejoins the new server value (ref updates in keep branch)', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    const select = screen.getByLabelText('Alert notifier') as HTMLSelectElement;
    // Edit → pending 'none'.
    fireEvent.change(select, { target: { value: 'none' } });
    // Concurrent tick to 'osascript' is KEPT (stays 'none'); ref advances to 'osascript'.
    act(() => seedAlertsConfig({ notifier: 'osascript' }));
    expect(select.value).toBe('none');
    // User manually rejoins the current server value → local === server (clean).
    fireEvent.change(select, { target: { value: 'osascript' } });
    expect(select.value).toBe('osascript');
    // A later tick must now ADOPT (proves the ref advanced in the keep branch;
    // a stale ref stuck at 'auto' would hold 'osascript' forever).
    act(() => seedAlertsConfig({ notifier: 'notify-send' }));
    expect(select.value).toBe('notify-send');
  });
});

// ---------------------------------------------------------------------------
// #513 S2 §1 — the field registry and its reducer.
//
// Roughly ten hand-coordinated per-field edit sites (a `useState`, a
// `lastSeen*` ref, a reconcile effect, a `*Dirty` derivation, an aggregation
// and a branch in `save()`) become one declarative registry consumed by one
// reducer. The properties below are what that buys, and each is stated as a
// property rather than as a walk-through of the implementation.
// ---------------------------------------------------------------------------
import {
  REGISTRY,
  type SettingsSources,
  type ServerField,
} from './settings/registry';
import {
  buildBody,
  dirtyIds,
  localActions,
  openForm,
  reconcileForm,
  setDraft,
  toggleStaged,
} from './settings/useSettingsForm';

function sources(patch: Partial<SettingsSources> = {}): SettingsSources {
  return {
    tz: 'local',
    tzPinned: false,
    alerts: {
      enabled: false,
      weekly_thresholds: [90, 95],
      five_hour_thresholds: [90, 95],
      budget_thresholds: [90, 100],
      budget_enabled: false,
      weekly_usd: null,
      projected_weekly_enabled: false,
      projected_budget_enabled: false,
      project_alerts_enabled: false,
      codex_budget_configured: false,
      codex_budget_alerts_enabled: false,
      codex_projected_enabled: false,
      notifier: 'auto',
      command_configured: false,
    },
    markersEnabled: true,
    liveTail: true,
    lanAuth: true,
    channel: 'stable',
    sortDefault: 'started desc',
    sessionsPerPage: 100,
    filterText: '',
    ...patch,
  };
}

function withBudget(weekly_usd: number | null, patch: Partial<SettingsSources> = {}) {
  const base = sources(patch);
  return { ...base, alerts: { ...base.alerts, weekly_usd } };
}

describe('canonical equality is declared once and used by BOTH dirty and reconcile', () => {
  it('holds an equivalent-but-lexically-different draft as CLEAN, and adopts a tick', () => {
    // The review's P1. A literal `draft !== toDraft(server)` comparison would
    // call this dirty, hold the tick, and leave the field reading dirty for
    // ever — the user typed the same amount, spelled differently.
    let form = openForm(withBudget(50));
    form = setDraft(form, 'budget.weekly_usd', '50.0');
    expect(dirtyIds(form)).not.toContain('budget.weekly_usd');
    form = reconcileForm(form, withBudget(60));
    expect(form.draft['budget.weekly_usd']).toBe('60');
    expect(dirtyIds(form)).not.toContain('budget.weekly_usd');
  });

  it('holds an INVALID draft and reports it dirty', () => {
    let form = openForm(withBudget(50));
    form = setDraft(form, 'budget.weekly_usd', 'abc');
    expect(dirtyIds(form)).toContain('budget.weekly_usd');
    form = reconcileForm(form, withBudget(60));
    expect(form.draft['budget.weekly_usd']).toBe('abc');
    expect(dirtyIds(form)).toContain('budget.weekly_usd');
  });

  it('treats a lexical alias of the sessions-per-page value as clean too', () => {
    let form = openForm(sources({ sessionsPerPage: 100 }));
    form = setDraft(form, 'prefs.sessionsPerPage', '0100');
    expect(dirtyIds(form)).not.toContain('prefs.sessionsPerPage');
    form = reconcileForm(form, sources({ sessionsPerPage: 250 }));
    expect(form.draft['prefs.sessionsPerPage']).toBe('250');
  });

  it('an empty weekly-budget draft is canonically null, so clearing a budget is dirty', () => {
    let form = openForm(withBudget(50));
    form = setDraft(form, 'budget.weekly_usd', '');
    expect(dirtyIds(form)).toContain('budget.weekly_usd');
    expect(buildBody(form)).toEqual({ budget: { weekly_usd: null } });
  });
});

describe('#258 re-seed conflict guard, at the reducer level', () => {
  it('keeps a pending edit when a concurrent SAME-field tick arrives', () => {
    let form = openForm(sources());
    form = setDraft(form, 'alerts.notifier', 'none');
    form = reconcileForm(form, sources({ alerts: { ...sources().alerts, notifier: 'osascript' } }));
    expect(form.draft['alerts.notifier']).toBe('none');
    expect(dirtyIds(form)).toContain('alerts.notifier');
  });

  it('adopts an UNRELATED field while holding a pending edit', () => {
    let form = openForm(sources());
    form = setDraft(form, 'dashboard.live_tail', false);
    form = reconcileForm(form, sources({ alerts: { ...sources().alerts, notifier: 'osascript' } }));
    expect(form.draft['dashboard.live_tail']).toBe(false);
    expect(form.draft['alerts.notifier']).toBe('osascript');
  });

  it('adopts a concurrent write to a DIFFERENT field while holding a pending edit', () => {
    let form = openForm(sources());
    form = setDraft(form, 'alerts.notifier', 'none');
    form = reconcileForm(form, sources({ alerts: { ...sources().alerts, enabled: true } }));
    expect(form.draft['alerts.enabled']).toBe(true);
    expect(form.draft['alerts.notifier']).toBe('none');
  });

  it('re-adopts after the user manually rejoins the new server value', () => {
    let form = openForm(sources());
    form = setDraft(form, 'alerts.notifier', 'none');
    form = reconcileForm(form, sources({ alerts: { ...sources().alerts, notifier: 'osascript' } }));
    expect(form.draft['alerts.notifier']).toBe('none');
    // The user types the current server value themselves → clean again.
    form = setDraft(form, 'alerts.notifier', 'osascript');
    expect(dirtyIds(form)).not.toContain('alerts.notifier');
    // A later tick must now ADOPT. A stale baseline would hold 'osascript'
    // for ever.
    form = reconcileForm(form, sources({ alerts: { ...sources().alerts, notifier: 'notify-send' } }));
    expect(form.draft['alerts.notifier']).toBe('notify-send');
  });

  it('re-seeds every field on open, so a discarded edit never survives a reopen', () => {
    let form = openForm(sources());
    form = setDraft(form, 'dashboard.live_tail', false);
    expect(dirtyIds(form)).toContain('dashboard.live_tail');
    form = openForm(sources());
    expect(form.draft['dashboard.live_tail']).toBe(true);
    expect(dirtyIds(form)).toEqual([]);
  });
});

describe('registry structure', () => {
  it('every id and every path is unique', () => {
    const ids = REGISTRY.map((f) => f.id);
    expect(new Set(ids).size).toBe(ids.length);
    const paths = REGISTRY.filter((f) => f.kind === 'server').map(
      (f) => (f as ServerField<unknown, unknown>).path,
    );
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('no declared path is a prefix of another', () => {
    // `budget.codex.alerts_enabled` sits under `budget`, and
    // `budget.weekly_usd` is its sibling — this is a real hazard, not a
    // hypothetical one, which is why §3.1's ancestor targets live in a
    // SEPARATE map rather than in the registry.
    const paths = REGISTRY.filter((f) => f.kind === 'server').map(
      (f) => (f as ServerField<unknown, unknown>).path,
    );
    for (const a of paths) {
      for (const b of paths) {
        if (a === b) continue;
        expect(b.startsWith(`${a}.`)).toBe(false);
      }
    }
  });

  it('excludes ephemeral UI state from the registry', () => {
    for (const id of [
      'testAxis', 'testMetric', 'testSubmitting', 'testError', 'testOk',
      'lanAuthRestartSaved', 'confirmDiscard',
    ]) {
      expect(REGISTRY.find((f) => f.id === id)).toBeUndefined();
    }
  });

  it('declares thirteen server fields, three browser fields and two staged actions', () => {
    expect(REGISTRY.filter((f) => f.kind === 'server')).toHaveLength(13);
    expect(REGISTRY.filter((f) => f.kind === 'browser')).toHaveLength(3);
    expect(REGISTRY.filter((f) => f.kind === 'stagedAction')).toHaveLength(2);
  });

  it('gives every entry one of the six sections and a scope word', () => {
    const sections = new Set(['display', 'sessions', 'alerts', 'viewer', 'access', 'restore']);
    for (const field of REGISTRY) {
      expect(sections.has(field.section)).toBe(true);
      expect(field.scope === 'browser' || field.scope === 'machine').toBe(true);
    }
  });
});

describe('the POST body is built from the registry', () => {
  it('builds a sparse body containing ONLY dirty server fields', () => {
    let form = openForm(sources());
    expect(buildBody(form)).toEqual({});
    form = setDraft(form, 'alerts.enabled', true);
    expect(buildBody(form)).toEqual({ alerts: { enabled: true } });
  });

  it('nests budget.codex leaves without clobbering sibling budget leaves (#134)', () => {
    let form = openForm(sources());
    form = setDraft(form, 'budget.codex.alerts_enabled', true);
    form = setDraft(form, 'budget.projected_enabled', true);
    expect(buildBody(form)).toEqual({
      budget: { projected_enabled: true, codex: { alerts_enabled: true } },
    });
  });

  it('carries the timezone target, not the radio mode', () => {
    let form = openForm(sources());
    form = setDraft(form, 'display.tz', { mode: 'custom', custom: 'America/New_York' });
    expect(buildBody(form)).toEqual({ display: { tz: 'America/New_York' } });
  });

  it('omits the display block entirely when the timezone is pinned by --tz', () => {
    let form = openForm(sources({ tzPinned: true }));
    form = setDraft(form, 'display.tz', { mode: 'utc', custom: '' });
    expect(buildBody(form)).toEqual({});
  });

  it('never puts a browser field or a staged action in the body', () => {
    let form = openForm(sources());
    form = setDraft(form, 'prefs.filterText', 'claude');
    form = toggleStaged(form, 'restore.tableSort');
    expect(buildBody(form)).toEqual({});
    expect(dirtyIds(form)).toEqual(
      expect.arrayContaining(['prefs.filterText', 'restore.tableSort']),
    );
  });
});

describe('local dispatches stay gated by each field own dirty flag', () => {
  it('emits nothing for an untouched browser field', () => {
    const form = openForm(sources());
    expect(localActions(form)).toEqual([]);
  });

  it('saving only a server field never resets an untouched preference', () => {
    let form = openForm(sources());
    form = setDraft(form, 'alerts.notifier', 'none');
    expect(localActions(form)).toEqual([]);
  });

  it('emits the sort dispatches only when the sort default itself changed', () => {
    let form = openForm(sources());
    form = setDraft(form, 'prefs.sortDefault', 'cost desc');
    expect(localActions(form)).toEqual([
      { type: 'SAVE_PREFS', patch: { sortDefault: 'cost desc' } },
      { type: 'SET_SORT', key: 'cost desc' },
      { type: 'SET_TABLE_SORT', table: 'sessions', override: null },
    ]);
  });

  it('emits both staged resets when both are staged', () => {
    let form = openForm(sources());
    form = toggleStaged(form, 'restore.tableSort');
    form = toggleStaged(form, 'restore.cardOrder');
    expect(localActions(form)).toEqual([
      { type: 'SET_TABLE_SORT', table: 'sessions', override: null },
      { type: 'CLEAR_TABLE_SORTS' },
      { type: 'RESET_PANEL_ORDER' },
    ]);
  });

  it('resets a staged action to false on open — it never persists across opens', () => {
    let form = openForm(sources());
    form = toggleStaged(form, 'restore.cardOrder');
    expect(dirtyIds(form)).toContain('restore.cardOrder');
    form = openForm(sources());
    expect(dirtyIds(form)).not.toContain('restore.cardOrder');
  });

  it('never invents a server value for a staged action', () => {
    const form = openForm(sources());
    expect(form.server).not.toHaveProperty('restore.tableSort');
    expect(form.server).not.toHaveProperty('restore.cardOrder');
  });
});
