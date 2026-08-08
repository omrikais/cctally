// Spec §6.5 — "Save preset…" inline popover triggered from the
// ActionBar. M2 replaces the M1 stubbed disabled button.
//
// Validation mirrors the Python handler: name 1-64 chars, no '/'.
// We pre-validate client-side so the user sees a tight feedback loop
// rather than a 400 round-trip, but the server is still the source of
// truth.
//
// Esc/Enter are handled locally on the input via onKeyDown so they
// don't fight with the share modal's overlay-scoped Esc binding
// (which would close the whole modal instead of just dismissing the
// popover). #503 S3 §2 reconciles that handler with confirmation
// ownership: while an overwrite confirmation is armed, Escape on the
// input cancels the CONFIRMATION, not the popover.
//
// Saving onto an existing name used to replace it silently. It now asks
// first, and `overwrite: true` is sent only after the user confirms —
// the server, which decides the collision under its writer lock, answers
// HTTP 409 `preset_name_conflict` otherwise.
import { useEffect, useState } from 'react';
import {
  listPresets, savePreset, PRESET_NAME_CONFLICT, ShareApiError,
} from './presetsApi';
import { dispatch } from '../store/store';
import { ConfirmAction, useConfirmHost } from './ConfirmAction';
import type { SharePanelId, ShareOptions } from './types';
import type { DashboardSelection } from '../types/envelope';

const SAVE_OVERWRITE_ID = 'share-save-overwrite';

interface Props {
  panel: SharePanelId;
  // #294 S5 §7 — the flow's captured source; stamped on the saved preset
  // (overwrites the (panel, name) record and updates its stored source).
  // Optional with a 'claude' default (compatibility path).
  source?: DashboardSelection;
  templateId: string;
  options: ShareOptions;
  // #503 S3 §2 — the names already taken in this panel. The popover never
  // received them, so it could not tell the user a save was about to replace
  // something. Supplied by a caller that already holds the list; otherwise
  // fetched here. Either way it is an OPTIMISATION, never the authority —
  // the list can go stale between its read and the write, so the server's
  // 409 arms the same confirmation.
  existingNames?: string[];
  // #503 S3 §2 — where focus goes after an overwrite CONFIRM. The confirm
  // button unmounts the moment the confirmation closes, so without a target
  // focus falls to <body>; the spec puts it on the Save trigger, which this
  // component does not own. Cancel is unaffected — it restores the initiating
  // control, which the host hook captured for itself.
  focusAfterConfirm?: () => HTMLElement | null;
  onSaved: () => void;
  onCancel: () => void;
}

export function SavePresetPopover({
  panel, source = 'claude', templateId, options, existingNames,
  focusAfterConfirm, onSaved, onCancel,
}: Props) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetchedNames, setFetchedNames] = useState<string[]>([]);
  const confirm = useConfirmHost();
  const taken = existingNames ?? fetchedNames;

  useEffect(() => {
    if (existingNames != null) return;
    const ac = new AbortController();
    listPresets({ signal: ac.signal })
      .then((resp) => setFetchedNames(Object.keys(resp.presets[panel] ?? {})))
      .catch(() => { /* non-fatal — the 409 path still covers it */ });
    return () => ac.abort();
  }, [panel, existingNames]);

  async function submit(overwrite = false) {
    const trimmed = name.trim();
    if (!trimmed) {
      setError('Name is required');
      return;
    }
    if (trimmed.length > 64) {
      setError('Name must be 64 characters or fewer');
      return;
    }
    if (trimmed.includes('/')) {
      setError("Name cannot contain '/'");
      return;
    }
    if (!overwrite && taken.includes(trimmed)) {
      confirm.arm(SAVE_OVERWRITE_ID);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await savePreset({
        panel,
        name: trimmed,
        template_id: templateId,
        options,
        source,
        ...(overwrite ? { overwrite: true } : {}),
      });
      dispatch({ type: 'SHOW_STATUS_TOAST', text: `Saved preset "${trimmed}"` });
      onSaved();
    } catch (err: unknown) {
      if (err instanceof ShareApiError && err.code === PRESET_NAME_CONFLICT) {
        // The preflight was stale, or this caller never had a list.
        confirm.arm(SAVE_OVERWRITE_ID);
        return;
      }
      const msg =
        err instanceof ShareApiError
          ? err.message ?? `HTTP ${err.status}`
          : (err as Error).message;
      setError(msg ?? 'Save failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="share-save-popover" role="dialog" aria-label="Save preset">
      <label className="share-save-label">
        Preset name
        <input
          type="text"
          className="share-save-input"
          autoFocus
          value={name}
          maxLength={64}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              e.stopPropagation();
              void submit();
            } else if (e.key === 'Escape') {
              // stopPropagation prevents the share modal's overlay-scope
              // Esc binding from closing the whole modal. This handler runs
              // BEFORE the document dispatcher, so it also has to honour an
              // armed confirmation rather than race it (#503 S3 §2): with a
              // confirmation open, Escape cancels that and leaves the
              // popover alone.
              e.preventDefault();
              e.stopPropagation();
              if (confirm.armed != null) confirm.cancel();
              else onCancel();
            }
          }}
        />
      </label>
      {error ? <div className="share-save-error" role="alert">{error}</div> : null}
      <div className="share-save-actions">
        <button type="button" onClick={onCancel} disabled={busy}>Cancel</button>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy}
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
      <ConfirmAction
        id={SAVE_OVERWRITE_ID}
        host={confirm}
        prompt={`"${name.trim()}" exists — saving replaces it`}
        confirmLabel="Replace"
        onConfirm={() => {
          confirm.close(focusAfterConfirm);
          void submit(true);
        }}
      />
    </div>
  );
}
