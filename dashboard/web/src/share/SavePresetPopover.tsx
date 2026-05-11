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
// popover).
import { useState } from 'react';
import { savePreset, ShareApiError } from './presetsApi';
import { dispatch } from '../store/store';
import type { SharePanelId, ShareOptions } from './types';

interface Props {
  panel: SharePanelId;
  templateId: string;
  options: ShareOptions;
  onSaved: () => void;
  onCancel: () => void;
}

export function SavePresetPopover({
  panel, templateId, options, onSaved, onCancel,
}: Props) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
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
    setBusy(true);
    setError(null);
    try {
      await savePreset({
        panel,
        name: trimmed,
        template_id: templateId,
        options,
      });
      dispatch({ type: 'SHOW_STATUS_TOAST', text: `Saved preset "${trimmed}"` });
      onSaved();
    } catch (err: unknown) {
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
              // Esc binding from closing the whole modal.
              e.preventDefault();
              e.stopPropagation();
              onCancel();
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
    </div>
  );
}
