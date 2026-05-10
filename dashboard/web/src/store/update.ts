// Update-subcommand action helpers (spec §6).
//
// These wrap the network calls so components stay declarative — a Skip
// button just calls `updateActions.skip(version)` and trusts the
// resulting `setState` dispatch to repaint the badge / modal correctly.
//
// Cooking note: /api/update/status returns the raw `state` and
// `suppress` shapes from disk (single-source-of-truth). The cooking
// (`available`, `update_command`, `prerelease_note`) happens client-side
// in this module — the predicate matches the Python
// `_format_update_check_json` byte-for-byte semantics. This avoids
// adding a parallel cooked endpoint and keeps the on-disk JSON schema
// (`update-state.json` / `update-suppress.json`) the only contract
// between Python and the dashboard.

import { dispatch, getState } from './store';
import type {
  UpdateMethod,
  UpdateState,
  UpdateSuppress,
  UpdateCheckStatus,
  UpdateRemindAfter,
} from './store';

const UPDATE_STATUS_URL = '/api/update/status';
const UPDATE_START_URL = '/api/update';
const UPDATE_DISMISS_URL = '/api/update/dismiss';

function _formatUpdateCommand(
  method: UpdateMethod,
  version: string | null = null,
): string | null {
  // Mirrors Python `_format_update_command` (bin/cctally:10236).
  if (method === 'brew') return 'brew update --quiet && brew upgrade cctally';
  if (method === 'npm') return `npm install -g cctally@${version ?? 'latest'}`;
  return null;
}

function _prereleaseNote(current: string | null | undefined): string | null {
  // Mirrors Python `_prerelease_note` (bin/cctally:10248). Exact-string
  // contract — the Python tests pin this wording, and we mirror it on
  // the JS side so design pinning stays single-source.
  if (!current || !current.includes('-')) return null;
  return (
    `You're on prerelease ${current}; this banner suggests stable.\n` +
    'To track prereleases, manage manually: npm install -g cctally@next'
  );
}

// Coerce a raw /api/update/status response into the `UpdateState` shape.
// The server returns `state` as the raw on-disk dict (or `{_error: ...}`
// when the file failed to load); coerceState normalizes that into our
// strongly-typed slice.
export function coerceUpdateState(
  raw: unknown,
  suppress: UpdateSuppress,
): UpdateState | null {
  if (!raw || typeof raw !== 'object') return null;
  const obj = raw as Record<string, unknown>;
  // `_error` sentinel from the server when load failed — surface
  // gracefully as no-update-info-available.
  if (typeof obj._error === 'string') {
    return {
      current_version: null,
      latest_version: null,
      available: false,
      method: 'unknown',
      update_command: null,
      release_notes_url: null,
      check_status: 'unavailable',
      checked_at_utc: null,
      prerelease_note: null,
    };
  }
  const current = typeof obj.current_version === 'string' ? obj.current_version : null;
  const latest = typeof obj.latest_version === 'string' ? obj.latest_version : null;
  const installRaw = obj.install;
  const install = (installRaw && typeof installRaw === 'object')
    ? (installRaw as Record<string, unknown>)
    : {};
  const methodRaw = typeof install.method === 'string' ? install.method : 'unknown';
  const method: UpdateMethod =
    methodRaw === 'brew' || methodRaw === 'npm' ? methodRaw : 'unknown';
  const checkStatusRaw = typeof obj.check_status === 'string' ? obj.check_status : null;
  const validStatuses: UpdateCheckStatus[] = [
    'ok', 'rate_limited', 'fetch_failed', 'parse_failed', 'unavailable',
  ];
  const check_status: UpdateCheckStatus | null =
    checkStatusRaw && (validStatuses as string[]).includes(checkStatusRaw)
      ? (checkStatusRaw as UpdateCheckStatus)
      : null;
  const checked_at_utc = typeof obj.checked_at_utc === 'string' ? obj.checked_at_utc : null;
  const release_notes_url = typeof obj.latest_version_url === 'string' ? obj.latest_version_url : null;
  const available = _cookAvailable(current, latest, suppress);
  return {
    current_version: current,
    latest_version: latest,
    available,
    method,
    update_command: _formatUpdateCommand(method, null),
    release_notes_url,
    check_status,
    checked_at_utc,
    prerelease_note: _prereleaseNote(current),
  };
}

export function coerceUpdateSuppress(raw: unknown): UpdateSuppress {
  if (!raw || typeof raw !== 'object') {
    return { skipped_versions: [], remind_after: null };
  }
  const obj = raw as Record<string, unknown>;
  const skipped = Array.isArray(obj.skipped_versions)
    ? obj.skipped_versions.filter((v): v is string => typeof v === 'string')
    : [];
  let remind: UpdateRemindAfter | null = null;
  if (obj.remind_after && typeof obj.remind_after === 'object') {
    const r = obj.remind_after as Record<string, unknown>;
    if (typeof r.version === 'string' && typeof r.until_utc === 'string') {
      remind = { version: r.version, until_utc: r.until_utc };
    }
  }
  return { skipped_versions: skipped, remind_after: remind };
}

// Cooked predicate. Mirrors the Python `_format_update_check_json`
// available calculation: semver-greater AND not in skipped_versions AND
// not inside an active remind window.
function _cookAvailable(
  current: string | null,
  latest: string | null,
  suppress: UpdateSuppress,
): boolean {
  if (!current || !latest) return false;
  if (!_semverGt(latest, current)) return false;
  if (suppress.skipped_versions.includes(latest)) return false;
  const remind = suppress.remind_after;
  if (remind && remind.version && remind.until_utc) {
    try {
      if (!_semverGt(latest, remind.version)) {
        const until = Date.parse(remind.until_utc);
        if (Number.isFinite(until) && Date.now() < until) {
          return false;
        }
      }
    } catch {
      /* malformed remind block — fall through to "available". */
    }
  }
  return true;
}

function _semverGt(a: string, b: string): boolean {
  const parse = (v: string): [number, number, number, string] => {
    const [core, pre = ''] = v.split('-', 2);
    const parts = core.split('.').map((n) => parseInt(n, 10));
    while (parts.length < 3) parts.push(0);
    return [parts[0] || 0, parts[1] || 0, parts[2] || 0, pre];
  };
  const [aMa, aMi, aPa, aPre] = parse(a);
  const [bMa, bMi, bPa, bPre] = parse(b);
  if (aMa !== bMa) return aMa > bMa;
  if (aMi !== bMi) return aMi > bMi;
  if (aPa !== bPa) return aPa > bPa;
  if (!aPre && bPre) return true;
  if (aPre && !bPre) return false;
  return aPre > bPre;
}

// ---------- Public actions ----------

export async function refreshUpdateState(): Promise<void> {
  // Best-effort fetch. A network error or non-2xx leaves state.update.state
  // as it was; the badge gates on `available` so an unavailable API
  // simply hides the chip until the next refresh.
  try {
    const r = await fetch(UPDATE_STATUS_URL);
    if (!r.ok) return;
    const body = await r.json();
    const suppress = coerceUpdateSuppress(body?.suppress);
    const state = coerceUpdateState(body?.state, suppress);
    dispatch({ type: 'SET_UPDATE_STATE', state, suppress });
    // Auto-close the running/success modal when the post-execvp refresh
    // shows current_version === latest_version (success completed).
    const slice = getState().update;
    if (
      slice.status === 'success' &&
      state?.current_version &&
      state.current_version === state.latest_version
    ) {
      dispatch({ type: 'CLOSE_UPDATE_MODAL' });
      dispatch({ type: 'RESET_UPDATE_RUN' });
    }
  } catch {
    // Swallow — network blip / dashboard restart in flight.
  }
}

export async function startUpdate(version?: string | null): Promise<void> {
  // Reset prior run state so a Retry after a failed run starts fresh.
  dispatch({ type: 'RESET_UPDATE_RUN' });
  dispatch({
    type: 'SET_UPDATE_STATUS',
    status: 'running',
    errorMessage: null,
  });
  dispatch({
    type: 'SET_UPDATE_RUN_ID',
    runId: null,
    startedAt: Date.now(),
  });
  try {
    const body = version ? JSON.stringify({ version }) : '{}';
    const r = await fetch(UPDATE_START_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    const json = await r.json().catch(() => ({}));
    if (r.status === 202 && typeof json.run_id === 'string') {
      dispatch({ type: 'SET_UPDATE_RUN_ID', runId: json.run_id });
      return;
    }
    if (r.status === 409 && typeof json.run_id_in_progress === 'string') {
      // Another run in flight — adopt that runId so we tail its stream.
      dispatch({ type: 'SET_UPDATE_RUN_ID', runId: json.run_id_in_progress });
      return;
    }
    dispatch({
      type: 'SET_UPDATE_STATUS',
      status: 'failed',
      errorMessage:
        typeof json.error === 'string'
          ? json.error
          : `failed to start update (HTTP ${r.status})`,
    });
  } catch (err) {
    dispatch({
      type: 'SET_UPDATE_STATUS',
      status: 'failed',
      errorMessage: 'network error: ' + (err as Error).message,
    });
  }
}

export async function skipUpdate(version: string | null): Promise<void> {
  if (!version) return;
  try {
    const r = await fetch(UPDATE_DISMISS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'skip', version }),
    });
    if (r.ok) {
      // Optimistic: hide the badge immediately; the canonical refresh
      // below repaints from server state if anything diverged.
      const slice = getState().update;
      if (slice.state) {
        dispatch({
          type: 'SET_UPDATE_STATE',
          state: { ...slice.state, available: false },
          suppress: {
            ...slice.suppress,
            skipped_versions: slice.suppress.skipped_versions.includes(version)
              ? slice.suppress.skipped_versions
              : [...slice.suppress.skipped_versions, version],
          },
        });
      }
      dispatch({ type: 'CLOSE_UPDATE_MODAL' });
    }
  } catch {
    // best-effort — refresh below covers reconciliation
  }
  await refreshUpdateState();
}

export async function remindUpdate(days: number): Promise<void> {
  try {
    const r = await fetch(UPDATE_DISMISS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'remind', days }),
    });
    if (r.ok) {
      const slice = getState().update;
      if (slice.state) {
        dispatch({
          type: 'SET_UPDATE_STATE',
          state: { ...slice.state, available: false },
          suppress: slice.suppress,
        });
      }
      dispatch({ type: 'CLOSE_UPDATE_MODAL' });
    }
  } catch {
    // best-effort
  }
  await refreshUpdateState();
}

export const updateActions = {
  refreshState: refreshUpdateState,
  start: startUpdate,
  skip: skipUpdate,
  remind: remindUpdate,
  open: () => dispatch({ type: 'OPEN_UPDATE_MODAL' }),
  close: () => dispatch({ type: 'CLOSE_UPDATE_MODAL' }),
};
