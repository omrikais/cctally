// Spec §6.6 + §11.3 — list saved presets across all panels with
// rename / delete affordances.
//
// Rename is implemented client-side as "save under new name, then
// delete old" so a mid-flight failure can't lose the preset. The
// server has no rename endpoint (per spec the contract is idempotent
// on (panel, name) — save is the only mutation surface), and this
// keeps the implementation honest about the failure mode.
//
// Esc closes the modal — registered at `modal` scope so we don't
// preempt the share modal's own overlay-scope Esc binding. (Visually
// this modal layers above the share modal, but functionally it's
// open/close-independent: opening it doesn't close the share modal,
// closing it returns focus to the dropdown trigger.)
import { useEffect, useMemo, useState } from 'react';
import {
  listPresets, deletePreset, savePreset, type PresetRecord, ShareApiError,
} from './presetsApi';
import type { SharePanelId } from './types';
import { dispatch } from '../store/store';
import { useKeymap } from '../hooks/useKeymap';
import { sharePanelLabel } from './panelLabels';

interface Props {
  open: boolean;
  onClose: () => void;
}

interface Row {
  panel: SharePanelId;
  name: string;
  record: PresetRecord;
}

export function ManagePresetsModal({ open, onClose }: Props) {
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const bindings = useMemo(
    () => open
      ? [{ key: 'Escape', scope: 'modal' as const, action: onClose }]
      : [],
    [open, onClose],
  );
  useKeymap(bindings);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    (async () => {
      try {
        const resp = await listPresets();
        if (cancelled) return;
        const flat: Row[] = [];
        for (const [panel, bucket] of Object.entries(resp.presets)) {
          for (const [name, record] of Object.entries(bucket)) {
            flat.push({ panel: panel as SharePanelId, name, record });
          }
        }
        flat.sort((a, b) =>
          a.panel === b.panel ? a.name.localeCompare(b.name) : a.panel.localeCompare(b.panel),
        );
        setRows(flat);
      } catch (err: unknown) {
        if (cancelled) return;
        const msg =
          err instanceof ShareApiError
            ? err.message ?? `HTTP ${err.status}`
            : (err as Error).message;
        setError(msg ?? 'Failed to load presets');
      }
    })();
    return () => { cancelled = true; };
  }, [open]);

  async function handleDelete(row: Row) {
    setBusy(true);
    setError(null);
    try {
      await deletePreset(row.panel, row.name);
      setRows((curr) =>
        curr.filter((r) => !(r.panel === row.panel && r.name === row.name)),
      );
      dispatch({ type: 'SHOW_STATUS_TOAST', text: `Deleted preset "${row.name}"` });
    } catch (err: unknown) {
      const msg =
        err instanceof ShareApiError
          ? err.message ?? `HTTP ${err.status}`
          : (err as Error).message;
      setError(msg ?? 'Delete failed');
    } finally {
      setBusy(false);
    }
  }

  async function handleRename(row: Row, nextName: string) {
    setBusy(true);
    setError(null);
    try {
      // Save under new name first; only delete old if save succeeds so
      // a mid-flight failure can't lose the preset.
      await savePreset({
        panel: row.panel,
        name: nextName,
        template_id: row.record.template_id,
        options: row.record.options,
      });
      await deletePreset(row.panel, row.name);
      setRows((curr) => curr.map((r) =>
        (r.panel === row.panel && r.name === row.name)
          ? { ...r, name: nextName }
          : r,
      ));
      dispatch({ type: 'SHOW_STATUS_TOAST', text: `Renamed to "${nextName}"` });
    } catch (err: unknown) {
      const msg =
        err instanceof ShareApiError
          ? err.message ?? `HTTP ${err.status}`
          : (err as Error).message;
      setError(msg ?? 'Rename failed');
    } finally {
      setBusy(false);
    }
  }

  if (!open) return null;
  return (
    <div
      className="share-manage-modal"
      role="dialog"
      aria-modal="true"
      aria-label="Manage presets"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="share-manage-header">
        <h2>Manage presets</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="share-manage-close"
        >
          ⤬
        </button>
      </div>
      {error ? (
        <div className="share-manage-error" role="alert">{error}</div>
      ) : null}
      {rows.length === 0 && !error ? (
        <p className="share-manage-empty">No saved presets yet.</p>
      ) : null}
      {rows.length > 0 ? (
        <table className="share-manage-table">
          <thead>
            <tr>
              <th>Panel</th>
              <th>Name</th>
              <th>Saved at</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <ManagePresetRow
                key={`${row.panel}/${row.name}`}
                row={row}
                busy={busy}
                onDelete={() => void handleDelete(row)}
                onRename={(next) => void handleRename(row, next)}
              />
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function ManagePresetRow({ row, busy, onDelete, onRename }: {
  row: Row;
  busy: boolean;
  onDelete: () => void;
  onRename: (next: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(row.name);

  const commitRename = () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === row.name) {
      setDraft(row.name);
      setEditing(false);
      return;
    }
    onRename(trimmed);
    setEditing(false);
  };

  return (
    <tr>
      <td>{sharePanelLabel(row.panel)}</td>
      <td>
        {editing ? (
          <input
            className="share-manage-name-input"
            autoFocus
            value={draft}
            disabled={busy}
            maxLength={64}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                commitRename();
              } else if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                setDraft(row.name);
                setEditing(false);
              }
            }}
          />
        ) : (
          <span>{row.name}</span>
        )}
      </td>
      <td>{row.record.saved_at}</td>
      <td className="share-manage-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            if (editing) {
              setDraft(row.name);
              setEditing(false);
            } else {
              setEditing(true);
            }
          }}
        >
          {editing ? 'Cancel' : 'Rename'}
        </button>
        <button type="button" disabled={busy} onClick={onDelete}>
          Delete
        </button>
      </td>
    </tr>
  );
}
